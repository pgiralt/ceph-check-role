import os
import inspect
from collections import OrderedDict

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.facts.namespace import PrefixFactNamespace
from ansible.module_utils.facts import ansible_collector, default_collectors


DOCUMENTATION = """
---
module:
ceph_check_role
description:
  - This module performs validity checks for a given set of ceph roles against
the hosts ansible_facts


options:
  role:
    description:
      - string (comma separated) containing the roles to validate the host's
        configuration against (mons,osds,rgws,mdss,iscsigws)
    required: true
    default: none
  mode:
    description:
      - string denoting either dev or prod to govern the strictness of the validity rules
    default: 'prod'
    required: false
  deployment:
    description:
      - string denoting the deployment type; either 'container' or 'rpm'
    default: 'rpm'
    required: false

requirements: ['ansible >= 2.6']

author:
  - 'Paul Cuzner'

"""


def netmask_to_cidr(netmask):
    """ convert dotted quad netmask to CIDR (int) notation """
    return sum([bin(int(x)).count('1') for x in netmask.split('.')])


def get_cpu_type(processor_list):
    """ Extract and return a list of processor names """
    # each processor has 3 items, id, manufacturer and model
    # we just want the model
    names = processor_list[2::3]
    return list(set(names))


def get_free_disks(devices, rotational=1):
    """
    Determine free disks i.e. unused disks on this host

    Args:
        devices: dict form ansible_facts containing all disk devices
        rotational: rotational state of the device 0=ssd, 1=spinner (default)

    Return:
        Dictionary indexed by device name of all free disk. Each member
        contains the same parameters as ansible provides

    Exceptions:
        None
    """

    free = {}
    for disk_id in devices:
        disk = devices[disk_id]

        # skip removable device entries (eg cd drives)
        if disk['removable'] == "1":
            continue
        # skip device-mapper devices
        if disk_id.startswith('dm-'):
            continue
        # skip disks that have partitions already
        if disk['partitions']:
            continue
        # skip lvm owned devices
        if disk['holders']:
            continue
        # skip child devices of software RAID
        if disk['links']['masters']:
            continue
        # skip USB devices
        if disk['host'].upper().startswith("USB"):
            continue

        if int(disk['rotational']) == rotational:
            if not disk['partitions']:
                free[disk_id] = disk

    return OrderedDict(sorted(free.items()))


def get_server_details(ansible_facts):
    """
    Get the server model from the facts

    Args:
        ansible_facts: host facts from the ansible collector (setup) run

    Return:
        vendor: Server vendor name
        model: Server model eg. PowerEdge R730

    Exceptions:
        None
    """

    # this is a simplistic first pass at putting this info together!
    vendor = ansible_facts['system_vendor']
    if ansible_facts["product_version"] == 'NA':
        model = ansible_facts["product_name"]
    else:
        model = ansible_facts["product_version"]

    return vendor, model


def get_network_info(ansible_facts):
    """
    Look at the ansible facts to extract subnet ranges and configuration
    information

    Args:
        ansible_facts: hosts facts from ansible

    Return:
        dictionary  - subnet (list)
                    - subnet _details (dict)

    Exceptions:
        None
    """

    subnets = set()
    subnet_details = {}

    valid_nics = [
        "ether",
        "bonding",
        "bridge",
        "infiniband"
    ]

    nic_blacklist = ('lo')
    nics = [nic for nic in ansible_facts.get('interfaces') if not nic.startswith(nic_blacklist)]

    # Now process the nic list again so we can lookup speeds against pnics
    for nic_id in nics:

        if nic_id not in ansible_facts:
            continue

        # filter out nic types that we're not interested in
        if ansible_facts[nic_id].get('type') not in valid_nics:
            continue

        # look for ipv4 information
        nic_config = ansible_facts[nic_id].get('ipv4', None)
        if nic_config:

            network = nic_config['network']
            cidr = netmask_to_cidr(nic_config['netmask'])
            net_str = '{}/{}'.format(network, cidr)
            subnets.add(net_str)

            if ansible_facts[nic_id].get('type') in ['ether', 'infiniband']:
                devs = [nic_id]
                speed = ansible_facts[nic_id].get('speed', 0)
                count = 1

            elif ansible_facts[nic_id].get("type") == "bridge":
                count = speed = 0
                devs = [d.replace('-', '_') for d in ansible_facts[nic_id]['interfaces']
                        if not d.startswith('vnet')]
                for n in devs:
                    if ansible_facts[n]['type'] == "bonding":
                        count += len(ansible_facts[n]['slaves'])
                        speed += ansible_facts[n]['speed']
                    elif ansible_facts[n]['type'] == "bridge":
                        count += len(ansible_facts[n]['interfaces'])
                    elif ansible_facts[n]['type'] == "ether":
                        count += 1

            elif ansible_facts[nic_id].get('type') == "bonding":
                devs = [d.replace('-', '_') for d in ansible_facts[nic_id]['slaves']]
                speed = ansible_facts[nic_id].get('speed', 0)
                count = len(devs)

            if speed and count:
                desc = "{} ({}x{}g)".format(net_str, count, (speed / (count * 1000)))
            else:
                desc = net_str

            subnet_details[net_str] = {
                "devices": devs,
                "speed": speed,
                "count": count,
                "desc": desc
            }

    return {
        "subnets": list(subnets),
        "subnet_details": subnet_details
    }


def summarize(facts):
    """
    Look at the ansible_facts and distill down to those settings that impact
    or influence ceph deployment

    Args:
        facts : dictionary containing ansible_facts for this host

    Return:
        summary (dict) containing summarized configuration information

    Exceptions:
        None
    """

    summary = {}
    summary['cpu_core_count'] = facts.get('processor_count', 0) * \
        facts.get('processor_threads_per_core', 1) * \
        facts.get('processor_cores', 0)

    summary['ram_mb'] = facts['memory_mb']['real']['total']

    # extract the stats the summary stats to validate against
    summary['cpu_type'] = get_cpu_type(facts.get('processor', []))
    summary['hdd'] = get_free_disks(facts['devices'])
    summary['ssd'] = get_free_disks(facts['devices'], rotational=0)
    summary['hdd_count'] = len(summary['hdd'])
    summary['ssd_count'] = len(summary['ssd'])
    summary['network'] = get_network_info(facts)
    summary['vendor'], summary['model'] = get_server_details(facts)

    return summary


class Checker(object):

    osd_bandwidth = {
        'hdd': 1000,
        'ssd': 5000
    }

    reqs = {
        "os": {"cpu": 2,
               "ram": 4096},
        "osds": {"cpu": .5,
                "ram": 3072},
        "mons": {"cpu": 2,
                "ram": 4096},
        "mdss": {"cpu": 2,
                "ram": 4096},
        "rgws": {"cpu": 4,
                "ram": 2048},
        "iscsigws": {"cpu": 4,
                  "ram": 16384}
    }

    fs_threshold = {
        "dev": {"free": 10, "severity": "warning"},
        "prod": {"free": 30, "severity": "error"}
    }

    def __init__(self, host_details, roles, deployment_type='rpm', mode='prod'):
        self.host_details = host_details
        # Let's assume(!) that the larger of the hdd/ssd number is the number of osd
        # devices
        self.osd_count = max(len(host_details['hdd']), len(host_details['ssd']))
        self.osd_media = 'hdd' if len(host_details['hdd']) > len(host_details['ssd']) else 'ssd'
        self.roles = roles.split(',')
        self.deployment_type = deployment_type
        self.mode = mode
        self.status_msgs = []
        self.status_checks = []

        # Look at network bandwidth from OSD perspective
        subnet_data = host_details['network']['subnet_details']
        self.net_max = max([subnet_data[subnet]['speed'] for subnet in subnet_data])

    @property
    def state(self):
        if self.mode == 'dev':
            # only critical error apply in dev mode
            if any(msg.startswith('critical') for msg in self.status_msgs):
                return 'NOTOK'
            else:
                return 'OK'
        else:
            if any(msg.startswith(('critical', 'error')) for msg in self.status_msgs):
                return 'NOTOK'
            else:
                return 'OK'

    def analyse(self):
        check_methods = [member for member in
                         [getattr(self, attr) for attr in dir(self)]
                         if inspect.ismethod(member) and
                         member.__name__.startswith("_check")]

        for checker in check_methods:
            checker()

    def _add_problem(self, severity, description):
        self.status_msgs.append('{}:{}'.format(severity, description))

    def _add_check(self, checkname):
        self.status_checks.append(checkname)

    def _check_collocation(self):
        self._add_check("_check_collocation")
        severity = 'warning' if self.mode == 'dev' else 'error'

        num_roles = len(self.roles)
        if num_roles > 1:
            if self.deployment_type == 'container':
                # any combination is OK for containers
                return
            else:
                if num_roles > 2:
                    self._add_problem(severity, "too many roles for RPM deployment mode")
                    return
                else:
                    if all(role in ['osds', 'rgws'] for role in self.roles):
                        return
                    else:
                        self._add_problem(severity,
                                          "requested roles ({}) may not coexist".format(','.join(self.roles)))

    def _check_osd(self):
        self._add_check("_check_osd")
        if 'osds' in self.roles and self.osd_count == 0:
            self._add_problem("critical", "OSD role without any free disks")

    def _check_network(self):
        self._add_check("_check_network")
        if 'osds' not in self.roles:
            return

        optimum_bandwidth = self.osd_count * self.osd_bandwidth[self.osd_media]
        if self.net_max < optimum_bandwidth:
            self._add_problem("warning", "network bandwith low for the number of potential OSDs")

    def _check_cpu(self):
        self._add_check("_check_cpu")
        available_cpu = self.host_details['cpu_core_count']

        for role in self.roles:
            if role == 'osds':
                available_cpu -= (self.osd_count * self.reqs[role]['cpu'])
            else:
                available_cpu -= (self.reqs[role]['cpu'])

        if available_cpu < self.reqs['os']['cpu']:
            self._add_problem('error', "#CPU's too low")

    def _check_rgw(self):
        self._add_check("_check_rgw")
        if 'rgws' not in self.roles:
            return

        # prod mode - we should have at least 1 x 10g link
        if self.net_max < 10000:
            self._add_problem("warning", "network bandwidth low for rgw role")

    def _check_ram(self):
        self._add_check("_check_ram")
        available_ram = self.host_details['ram_mb']

        for role in self.roles:
            if role == 'osds':
                available_ram -= (self.osd_count * self.reqs[role]['ram'])
            else:
                available_ram -= self.reqs[role]['ram']

        if available_ram < self.reqs['os']['ram']:
            self._add_problem('error', 'RAM too low')

    def _check_mon_freespace(self):
        self._add_check("check mon freespace")
        if 'mons' in self.roles:
            var_lib = os.statvfs('/var/lib')
            free_bytes = var_lib.f_bsize * var_lib.f_bfree
            if free_bytes / 1024**3 < self.fs_threshold[self.mode]["free"]:
                self._add_problem(self.fs_threshold[self.mode]['severity'],
                                  "Freespace on /var/lib is too low "
                                  "(<{}GB)".format(self.fs_threshold[self.mode]["free"]))


def run_module():

    fields = dict(
        role=dict(
            type='str',
            required=True),
        mode=dict(
            type='str',
            choices=['prod', 'dev'],
            default='prod',
            required=False),
        deployment=dict(
            type='str',
            choices=['container', 'rpm'],
            default='rpm',
            required=False)
    )

    module = AnsibleModule(argument_spec=fields,
                           supports_check_mode=False)

    # Define the ansible collector logic, as used by the ansible "setup" module
    all_collector_classes = default_collectors.collectors
    minimal_gather_subset = frozenset(['apparmor', 'caps', 'cmdline', 'date_time',
                                       'distribution', 'dns', 'env', 'fips', 'local',
                                       'lsb', 'pkg_mgr', 'platform', 'python', 'selinux',
                                       'service_mgr', 'ssh_pub_keys', 'user'])

    namespace = PrefixFactNamespace(namespace_name='ansible',
                                    prefix='')

    fact_collector = \
        ansible_collector.get_ansible_collector(all_collector_classes=all_collector_classes,
                                                namespace=namespace,
                                                filter_spec="*",
                                                gather_subset=['all'],
                                                gather_timeout=10,
                                                minimal_gather_subset=minimal_gather_subset)

    # Get the facts from the host
    ansible_facts = fact_collector.collect(module=module)

    role = module.params.get('role')
    mode = module.params.get('mode')
    deployment_type = module.params.get('deployment')

    summary = summarize(ansible_facts)
    checker = Checker(host_details=summary, roles=role, deployment_type=deployment_type, mode=mode)
    checker.analyse()

    module.exit_json(
        changed=False,
        data={
            "role": role,
            "mode": mode,
            "deployment_type": deployment_type,
            'summary_facts': summary,
            'status_msgs': sorted(checker.status_msgs),
            'status': checker.state
        }
    )


def main():
    run_module()


if __name__ == "__main__":
    main()
