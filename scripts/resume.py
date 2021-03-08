#!/usr/bin/env python3

# Copyright 2017 SchedMD LLC.
# Modified for use with the Slurm Resource Manager.
#
# Copyright 2015 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import httplib2
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from itertools import groupby, chain

import googleapiclient.discovery
from google.auth import compute_engine
import google_auth_httplib2
from googleapiclient.http import set_user_agent

import util

PLACEMENT_MAX_CNT = 22

cfg = util.Config.load_config(Path(__file__).with_name('config.yaml'))

SCONTROL = Path(cfg.slurm_cmd_path or '')/'scontrol'
LOGFILE = (Path(cfg.log_dir or '')/Path(__file__).name).with_suffix('.log')
SCRIPTS_DIR = Path(__file__).parent.resolve()

TOT_REQ_CNT = 1000

instances = {}

if cfg.google_app_cred_path:
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = cfg.google_app_cred_path

def wait_for_operation(compute, operation):
    print('Waiting for operation to finish...')
    project = cfg.project
    while True:
        result = None
        if 'zone' in operation:
            result = compute.zoneOperations().get(
                project=project,
                zone=operation['zone'].split('/')[-1],
                operation=operation['name']).execute()
        elif 'region' in operation:
            result = compute.regionOperations().get(
                project=project,
                region=operation['region'].split('/')[-1],
                operation=operation['name']).execute()
        else:
            result = compute.globalOperations().get(
                project=project,
                operation=operation['name']).execute()

        if result['status'] == 'DONE':
            print("done.")
            return result

        time.sleep(1)
# [END wait_for_operation]


def create_instance(compute, instance_def, node_list, placement_group_name):

    pid = util.get_pid(node_list[0])
    # Configure the machine

    meta_files = {
        'config': SCRIPTS_DIR/'config.yaml',
        'util-script': SCRIPTS_DIR/'util.py',
        'startup-script': SCRIPTS_DIR/'startup.sh',
        'setup-script': SCRIPTS_DIR/'setup.py',
    }
    custom_compute = SCRIPTS_DIR/'custom-compute-install'
    if custom_compute.exists():
        meta_files['custom-compute-install'] = str(custom_compute)

    config = {
        'name': 'notused',
        'machineType': instance_def.machine_type,

        # Specify the boot disk and the image to use as a source.
        'disks': [{
            'boot': True,
            'autoDelete': True,
            'initializeParams': {
                'sourceImage': instance_def.image,
                'diskType': instance_def.compute_disk_type,
                'diskSizeGb': instance_def.compute_disk_size_gb
            }
        }],

        # Specify a network interface
        'networkInterfaces': [{
            'subnetwork': (
                "projects/{}/regions/{}/subnetworks/{}".format(
                    cfg.shared_vpc_host_project or cfg.project,
                    instance_def.region,
                    (instance_def.vpc_subnet
                     or f'{cfg.cluster_name}-{instance_def.region}'))
            ),
        }],

        # Allow the instance to access cloud storage and logging.
        'serviceAccounts': [{
            'email': cfg.compute_node_service_account,
            'scopes': cfg.compute_node_scopes
        }],

        'tags': {'items': ['compute']},

        'metadata': {
            'items': [
                {'key': 'enable-oslogin',
                 'value': 'TRUE'},
                {'key': 'VmDnsSetting',
                 'value': 'GlobalOnly'},
                {'key': 'terraform',
                 'value': 'TRUE'},
                *[{'key': k, 'value': Path(v).read_text()} for k, v in meta_files.items()]
            ]
        }
    }

    if placement_group_name is not None:
        config['scheduling'] = {
            'onHostMaintenance': 'TERMINATE',
            'automaticRestart': False
        }
        config['resourcePolicies'] = [placement_group_name]

    if instance_def.gpu_type:
        config['guestAccelerators'] = [{
            'acceleratorCount': instance_def.gpu_count,
            'acceleratorType': instance_def.gpu_type
        }]

        config['scheduling'] = {'onHostMaintenance': 'TERMINATE'}

    if instance_def.preemptible_bursting:
        config['scheduling'] = {
            'preemptible': True,
            'onHostMaintenance': 'TERMINATE',
            'automaticRestart': False
        }

    if instance_def.compute_labels:
        config['labels'] = instance_def.compute_labels

    if instance_def.cpu_platform:
        config['minCpuPlatform'] = instance_def.cpu_platform

    if cfg.external_compute_ips:
        config['networkInterfaces'][0]['accessConfigs'] = [
            {'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}
        ]

    perInstanceProperties = {k: {} for k in node_list}
    body = {
        'count': len(node_list),
        'instanceProperties': config,
        'perInstanceProperties': perInstanceProperties,
    }

    if instance_def.regional_capacity:
        if instance_def.regional_policy:
            body['locationPolicy'] = instance_def.regional_policy
        op = compute.regionInstances().bulkInsert(
            project=cfg.project, region=instance_def.region,
            body=body)
        return op.execute()

    return compute.instances().bulkInsert(
        project=cfg.project, zone=instance_def.zone,
        body=body).execute()
# [END create_instance]


def add_instances(node_chunk):

    node_list = node_chunk['nodes']
    pg_name = None
    if 'pg' in node_chunk:
        pg_name = node_chunk['pg']
    log.debug(f"node_list:{node_list} pg:{pg_name}")

    auth_http = None
    if not cfg.google_app_cred_path:
        http = set_user_agent(httplib2.Http(),
                              "Slurm_GCP_Scripts/1.1 (GPN:SchedMD)")
        creds = compute_engine.Credentials()
        auth_http = google_auth_httplib2.AuthorizedHttp(creds, http=http)
    compute = googleapiclient.discovery.build('compute', 'alpha',
                                              http=auth_http,
                                              cache_discovery=False)
    pid = util.get_pid(node_list[0])
    instance_def = cfg.instance_defs[pid]

    try:
        operation = create_instance(compute, instance_def, node_list, pg_name)
        wait_for_operation(compute, operation)
    except Exception as e:
        log.error(f"failed to add {node_list[0]}*{len(node_list)} to slurm, {e}")
        down_nodes(node_list, "resume.py failed, see resume.log")
# [END add_instances]


def down_nodes(node_list, reason):
    """ set nodes in node_list down with given reason """
    with tempfile.NamedTemporaryFile(mode='w+t') as f:
        f.writelines("\n".join(node_list))
        f.flush()
        hostlist = util.run(f"{SCONTROL} show hostlist {f.name}",
                            check=True, get_stdout=True).stdout
    util.run(
        f"{SCONTROL} update nodename={hostlist} state=down reason='{reason}'")
# [END down_nodes]


def create_placement_groups(arg_job_id, vm_count, region):
    log.debug(f"Creating PG: {arg_job_id} vm_count:{vm_count} region:{region}")

    pg_names = []
    pg_ops = []
    pg_index = 0

    auth_http = None
    if not cfg.google_app_cred_path:
        http = set_user_agent(httplib2.Http(),
                              "Slurm_GCP_Scripts/1.1 (GPN:SchedMD)")
        creds = compute_engine.Credentials()
        auth_http = google_auth_httplib2.AuthorizedHttp(creds, http=http)
    compute = googleapiclient.discovery.build('compute', 'alpha',
                                              http=auth_http,
                                              cache_discovery=False)

    for i in range(vm_count):
        if i % PLACEMENT_MAX_CNT:
            continue
        pg_index += 1
        pg_name = f'{cfg.cluster_name}-{arg_job_id}-{pg_index}'
        pg_names.append(pg_name)

        config = {
            'name': pg_name,
            'region': region,
            'groupPlacementPolicy': {
                "collocation": "COLLOCATED",
                "vmCount": min(vm_count - i, PLACEMENT_MAX_CNT)
             }
        }

        pg_ops.append(compute.resourcePolicies().insert(
            project=cfg.project,
            region=region,
            body=config).execute())

    for operation in pg_ops:
        wait_for_operation(compute, operation)

    return pg_names
# [END create_placement_groups]


def main(arg_nodes, arg_job_id):
    log.info(f"Bursting out: {arg_nodes} {arg_job_id}")
    # Get node list
    nodes_str = util.run(f"{SCONTROL} show hostnames {arg_nodes}",
                         check=True, get_stdout=True).stdout
    node_list = sorted(nodes_str.splitlines(), key=util.get_pid)

    placement_groups = None
    pid = util.get_pid(node_list[0])
    if (arg_job_id and not cfg.instance_defs[pid].exclusive):
        # Don't create from calls by PrologSlurmctld
        return

    nodes_by_pid = {k: tuple(nodes)
                    for k, nodes in groupby(node_list, util.get_pid)}

    if not arg_job_id:
        for pid in [pid for pid in nodes_by_pid
                    if cfg.instance_defs[pid].exclusive]:
            # Node was created by PrologSlurmctld, skip for ResumeProgram.
            del nodes_by_pid[pid]

    if (arg_job_id and
            cfg.instance_defs[pid].enable_placement and
            cfg.instance_defs[pid].machine_type.split('-')[0] == "c2" and
            len(node_list) > 1):
        log.debug(f"creating placement group for {arg_job_id}")
        placement_groups = create_placement_groups(
            arg_job_id, len(node_list), cfg.instance_defs[pid].region)
        time.sleep(5)

    def chunks(lst, pg_names):
        """ group list into chunks of max size n """
        n = 1000
        if pg_names:
            n = PLACEMENT_MAX_CNT

        pg_index = 0
        for i in range(0, len(lst), n):
            chunk = dict(nodes=lst[i:i+n])
            if pg_names:
                chunk['pg'] = pg_names[pg_index]
                pg_index += 1
            yield chunk
    # concurrently add nodes grouped by instance_def (pid), max 1000
    with ThreadPoolExecutor() as exe:
        node_chunks = chain.from_iterable(
            map(partial(chunks, pg_names=placement_groups),
                nodes_by_pid.values()))
        exe.map(add_instances, node_chunks)

    log.info("done adding instances")
# [END main]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('args', nargs='+', help="nodes [jobid]")
    parser.add_argument('--debug', '-d', dest='debug', action='store_true',
                        help='Enable debugging output')

    job_id = 0
    nodes = ""

    if "SLURM_JOB_NODELIST" in os.environ:
        args = parser.parse_args(sys.argv[1:] +
                                 [os.environ['SLURM_JOB_NODELIST'],
                                  os.environ['SLURM_JOB_ID']])
    else:
        args = parser.parse_args()

    nodes = args.args[0]
    if len(args.args) > 1:
        job_id = args.args[1]

    if args.debug:
        util.config_root_logger(level='DEBUG', util_level='DEBUG',
                                logfile=LOGFILE)
    else:
        util.config_root_logger(level='INFO', util_level='ERROR',
                                logfile=LOGFILE)
    log = logging.getLogger(Path(__file__).name)
    sys.excepthook = util.handle_exception

    new_yaml = Path(__file__).with_name('config.yaml.new')
    if (not cfg.instance_defs or cfg.partitions) and not new_yaml.exists():
        log.info(f"partition declarations in config.yaml have been converted to a new format and saved to {new_yaml}. Replace config.yaml as soon as possible.")
        cfg.save_config(new_yaml)

    main(nodes, job_id)
