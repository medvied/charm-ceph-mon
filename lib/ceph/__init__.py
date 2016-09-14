# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import ctypes
import json
import subprocess
import time
import os
import re
import sys
import errno
import shutil

from charmhelpers.core import hookenv

from charmhelpers.core.host import (
    mkdir,
    chownr,
    service_restart,
    lsb_release,
    cmp_pkgrevno, service_stop, mounts)
from charmhelpers.core.hookenv import (
    log,
    ERROR,
    cached,
    status_set,
    WARNING, DEBUG)
from charmhelpers.core.services import render_template
from charmhelpers.fetch import (
    apt_cache
)

from charmhelpers.contrib.storage.linux.utils import (
    is_block_device,
    zap_disk,
    is_device_mounted)
from utils import (
    get_unit_hostname,
)


LEADER = 'leader'
PEON = 'peon'
QUORUM = [LEADER, PEON]

PACKAGES = ['ceph', 'gdisk', 'ntp', 'btrfs-tools', 'python-ceph', 'xfsprogs']

LinkSpeed = {
    "BASE_10": 10,
    "BASE_100": 100,
    "BASE_1000": 1000,
    "GBASE_10": 10000,
    "GBASE_40": 40000,
    "GBASE_100": 100000,
    "UNKNOWN": None
}

# Mapping of adapter speed to sysctl settings
NETWORK_ADAPTER_SYSCTLS = {
    # 10Gb
    LinkSpeed["GBASE_10"]: {
        'net.core.rmem_default': 524287,
        'net.core.wmem_default': 524287,
        'net.core.rmem_max': 524287,
        'net.core.wmem_max': 524287,
        'net.core.optmem_max': 524287,
        'net.core.netdev_max_backlog': 300000,
        'net.ipv4.tcp_rmem': '10000000 10000000 10000000',
        'net.ipv4.tcp_wmem': '10000000 10000000 10000000',
        'net.ipv4.tcp_mem': '10000000 10000000 10000000'
    },
    # Mellanox 10/40Gb
    LinkSpeed["GBASE_40"]: {
        'net.ipv4.tcp_timestamps': 0,
        'net.ipv4.tcp_sack': 1,
        'net.core.netdev_max_backlog': 250000,
        'net.core.rmem_max': 4194304,
        'net.core.wmem_max': 4194304,
        'net.core.rmem_default': 4194304,
        'net.core.wmem_default': 4194304,
        'net.core.optmem_max': 4194304,
        'net.ipv4.tcp_rmem': '4096 87380 4194304',
        'net.ipv4.tcp_wmem': '4096 65536 4194304',
        'net.ipv4.tcp_low_latency': 1,
        'net.ipv4.tcp_adv_win_scale': 1
    }
}


def save_sysctls(sysctl_dict, save_location):
    """
    Persist the sysctls to the hard drive.
    :param sysctl_dict: dict
    :param save_location: path to save the settings to
    :raise: IOError if anything goes wrong with writing.
    """
    try:
        # Persist the settings for reboots
        with open(save_location, "w") as fd:
            for key, value in sysctl_dict.items():
                fd.write("{}={}\n".format(key, value))

    except IOError as e:
        log("Unable to persist sysctl settings to {}.  Error {}".format(
            save_location, e.message), level=ERROR)
        raise


def tune_nic(network_interface):
    """
    This will set optimal sysctls for the particular network adapter.
    :param network_interface: string The network adapter name.
    """
    speed = get_link_speed(network_interface)
    if speed in NETWORK_ADAPTER_SYSCTLS:
        status_set('maintenance', 'Tuning device {}'.format(
            network_interface))
        sysctl_file = os.path.join(
            os.sep,
            'etc',
            'sysctl.d',
            '51-ceph-osd-charm-{}.conf'.format(network_interface))
        try:
            log("Saving sysctl_file: {} values: {}".format(
                sysctl_file, NETWORK_ADAPTER_SYSCTLS[speed]),
                level=DEBUG)
            save_sysctls(sysctl_dict=NETWORK_ADAPTER_SYSCTLS[speed],
                         save_location=sysctl_file)
        except IOError as e:
            log("Write to /etc/sysctl.d/51-ceph-osd-charm-{} "
                "failed. {}".format(network_interface, e.message),
                level=ERROR)

        try:
            # Apply the settings
            log("Applying sysctl settings", level=DEBUG)
            subprocess.check_output(["sysctl", "-p", sysctl_file])
        except subprocess.CalledProcessError as err:
            log('sysctl -p {} failed with error {}'.format(sysctl_file,
                                                           err.output),
                level=ERROR)
    else:
        log("No settings found for network adapter: {}".format(
            network_interface), level=DEBUG)


def get_link_speed(network_interface):
    """
    This will find the link speed for a given network device.  Returns None
    if an error occurs.
    :param network_interface: string The network adapter interface.
    :return: LinkSpeed
    """
    speed_path = os.path.join(os.sep, 'sys', 'class', 'net',
                              network_interface, 'speed')
    # I'm not sure where else we'd check if this doesn't exist
    if not os.path.exists(speed_path):
        return LinkSpeed["UNKNOWN"]

    try:
        with open(speed_path, 'r') as sysfs:
            nic_speed = sysfs.readlines()

            # Did we actually read anything?
            if not nic_speed:
                return LinkSpeed["UNKNOWN"]

            # Try to find a sysctl match for this particular speed
            for name, speed in LinkSpeed.items():
                if speed == int(nic_speed[0].strip()):
                    return speed
            # Default to UNKNOWN if we can't find a match
            return LinkSpeed["UNKNOWN"]
    except IOError as e:
        log("Unable to open {path} because of error: {error}".format(
            path=speed_path,
            error=e.message), level='error')
        return LinkSpeed["UNKNOWN"]


def persist_settings(settings_dict):
    # Write all settings to /etc/hdparm.conf
    """
        This will persist the hard drive settings to the /etc/hdparm.conf file
        The settings_dict should be in the form of {"uuid": {"key":"value"}}
        :param settings_dict: dict of settings to save
    """
    hdparm_path = os.path.join(os.sep, 'etc', 'hdparm.conf')
    try:
        with open(hdparm_path, 'w') as hdparm:
            hdparm.write(render_template('hdparm.conf', settings_dict))
    except IOError as err:
        log("Unable to open {path} because of error: {error}".format(
            path=hdparm_path,
            error=err.message), level=ERROR)


def set_max_sectors_kb(dev_name, max_sectors_size):
    """
    This function sets the max_sectors_kb size of a given block device.
    :param dev_name: Name of the block device to query
    :param max_sectors_size: int of the max_sectors_size to save
    """
    max_sectors_kb_path = os.path.join('sys', 'block', dev_name, 'queue',
                                       'max_sectors_kb')
    try:
        with open(max_sectors_kb_path, 'w') as f:
            f.write(max_sectors_size)
    except IOError as e:
        log('Failed to write max_sectors_kb to {}. Error: {}'.format(
            max_sectors_kb_path, e.message), level=ERROR)


def get_max_sectors_kb(dev_name):
    """
    This function gets the max_sectors_kb size of a given block device.
    :param dev_name: Name of the block device to query
    :return: int which is either the max_sectors_kb or 0 on error.
    """
    max_sectors_kb_path = os.path.join('sys', 'block', dev_name, 'queue',
                                       'max_sectors_kb')

    # Read in what Linux has set by default
    if os.path.exists(max_sectors_kb_path):
        try:
            with open(max_sectors_kb_path, 'r') as f:
                max_sectors_kb = f.read().strip()
                return int(max_sectors_kb)
        except IOError as e:
            log('Failed to read max_sectors_kb to {}. Error: {}'.format(
                max_sectors_kb_path, e.message), level=ERROR)
            # Bail.
            return 0
    return 0


def get_max_hw_sectors_kb(dev_name):
    """
    This function gets the max_hw_sectors_kb for a given block device.
    :param dev_name: Name of the block device to query
    :return: int which is either the max_hw_sectors_kb or 0 on error.
    """
    max_hw_sectors_kb_path = os.path.join('sys', 'block', dev_name, 'queue',
                                          'max_hw_sectors_kb')
    # Read in what the hardware supports
    if os.path.exists(max_hw_sectors_kb_path):
        try:
            with open(max_hw_sectors_kb_path, 'r') as f:
                max_hw_sectors_kb = f.read().strip()
                return int(max_hw_sectors_kb)
        except IOError as e:
            log('Failed to read max_hw_sectors_kb to {}. Error: {}'.format(
                max_hw_sectors_kb_path, e.message), level=ERROR)
            return 0
    return 0


def set_hdd_read_ahead(dev_name, read_ahead_sectors=256):
    """
    This function sets the hard drive read ahead.
    :param dev_name: Name of the block device to set read ahead on.
    :param read_ahead_sectors: int How many sectors to read ahead.
    """
    try:
        # Set the read ahead sectors to 256
        log('Setting read ahead to {} for device {}'.format(
            read_ahead_sectors,
            dev_name))
        subprocess.check_output(['hdparm',
                                 '-a{}'.format(read_ahead_sectors),
                                 dev_name])
    except subprocess.CalledProcessError as e:
        log('hdparm failed with error: {}'.format(e.output),
            level=ERROR)


def get_block_uuid(block_dev):
    """
    This queries blkid to get the uuid for a block device.
    :param block_dev: Name of the block device to query.
    :return: The UUID of the device or None on Error.
    """
    try:
        block_info = subprocess.check_output(
            ['blkid', '-o', 'export', block_dev])
        for tag in block_info.split('\n'):
            parts = tag.split('=')
            if parts[0] == 'UUID':
                return parts[1]
        return None
    except subprocess.CalledProcessError as err:
        log('get_block_uuid failed with error: {}'.format(err.output),
            level=ERROR)
        return None


def check_max_sectors(save_settings_dict,
                      block_dev,
                      uuid):
    """
    Tune the max_hw_sectors if needed.
    make sure that /sys/.../max_sectors_kb matches max_hw_sectors_kb or at
    least 1MB for spinning disks
    If the box has a RAID card with cache this could go much bigger.
    :param save_settings_dict: The dict used to persist settings
    :param block_dev: A block device name: Example: /dev/sda
    :param uuid: The uuid of the block device
    """
    dev_name = None
    path_parts = os.path.split(block_dev)
    if len(path_parts) == 2:
        dev_name = path_parts[1]
    else:
        log('Unable to determine the block device name from path: {}'.format(
            block_dev))
        # Play it safe and bail
        return
    max_sectors_kb = get_max_sectors_kb(dev_name=dev_name)
    max_hw_sectors_kb = get_max_hw_sectors_kb(dev_name=dev_name)

    if max_sectors_kb < max_hw_sectors_kb:
        # OK we have a situation where the hardware supports more than Linux is
        # currently requesting
        config_max_sectors_kb = hookenv.config('max-sectors-kb')
        if config_max_sectors_kb < max_hw_sectors_kb:
            # Set the max_sectors_kb to the config.yaml value if it is less
            # than the max_hw_sectors_kb
            log('Setting max_sectors_kb for device {} to {}'.format(
                dev_name, config_max_sectors_kb))
            save_settings_dict[
                "drive_settings"][uuid][
                "read_ahead_sect"] = config_max_sectors_kb
            set_max_sectors_kb(dev_name=dev_name,
                               max_sectors_size=config_max_sectors_kb)
        else:
            # Set to the max_hw_sectors_kb
            log('Setting max_sectors_kb for device {} to {}'.format(
                dev_name, max_hw_sectors_kb))
            save_settings_dict[
                "drive_settings"][uuid]['read_ahead_sect'] = max_hw_sectors_kb
            set_max_sectors_kb(dev_name=dev_name,
                               max_sectors_size=max_hw_sectors_kb)
    else:
        log('max_sectors_kb match max_hw_sectors_kb.  No change needed for '
            'device: {}'.format(block_dev))


def tune_dev(block_dev):
    """
    Try to make some intelligent decisions with HDD tuning.  Future work will
    include optimizing SSDs.
    This function will change the read ahead sectors and the max write
    sectors for each block device.
    :param block_dev: A block device name: Example: /dev/sda
    """
    uuid = get_block_uuid(block_dev)
    if uuid is None:
        log('block device {} uuid is None.  Unable to save to '
            'hdparm.conf'.format(block_dev), level=DEBUG)
    save_settings_dict = {}
    log('Tuning device {}'.format(block_dev))
    status_set('maintenance', 'Tuning device {}'.format(block_dev))
    set_hdd_read_ahead(block_dev)
    save_settings_dict["drive_settings"] = {}
    save_settings_dict["drive_settings"][uuid] = {}
    save_settings_dict["drive_settings"][uuid]['read_ahead_sect'] = 256

    check_max_sectors(block_dev=block_dev,
                      save_settings_dict=save_settings_dict,
                      uuid=uuid)

    persist_settings(settings_dict=save_settings_dict)
    status_set('maintenance', 'Finished tuning device {}'.format(block_dev))


def ceph_user():
    if get_version() > 1:
        return 'ceph'
    else:
        return "root"


class CrushLocation(object):
    def __init__(self,
                 name,
                 identifier,
                 host,
                 rack,
                 row,
                 datacenter,
                 chassis,
                 root):
        self.name = name
        self.identifier = identifier
        self.host = host
        self.rack = rack
        self.row = row
        self.datacenter = datacenter
        self.chassis = chassis
        self.root = root

    def __str__(self):
        return "name: {} id: {} host: {} rack: {} row: {} datacenter: {} " \
               "chassis :{} root: {}".format(self.name, self.identifier,
                                             self.host, self.rack, self.row,
                                             self.datacenter, self.chassis,
                                             self.root)

    def __eq__(self, other):
        return not self.name < other.name and not other.name < self.name

    def __ne__(self, other):
        return self.name < other.name or other.name < self.name

    def __gt__(self, other):
        return self.name > other.name

    def __ge__(self, other):
        return not self.name < other.name

    def __le__(self, other):
        return self.name < other.name


def get_osd_tree(service):
    """
    Returns the current osd map in JSON.
    :return: List. :raise: ValueError if the monmap fails to parse.
      Also raises CalledProcessError if our ceph command fails
    """
    try:
        tree = subprocess.check_output(
            ['ceph', '--id', service,
             'osd', 'tree', '--format=json'])
        try:
            json_tree = json.loads(tree)
            crush_list = []
            # Make sure children are present in the json
            if not json_tree['nodes']:
                return None
            child_ids = json_tree['nodes'][0]['children']
            for child in json_tree['nodes']:
                if child['id'] in child_ids:
                    crush_list.append(
                        CrushLocation(
                            name=child.get('name'),
                            identifier=child['id'],
                            host=child.get('host'),
                            rack=child.get('rack'),
                            row=child.get('row'),
                            datacenter=child.get('datacenter'),
                            chassis=child.get('chassis'),
                            root=child.get('root')
                        )
                    )
            return crush_list
        except ValueError as v:
            log("Unable to parse ceph tree json: {}. Error: {}".format(
                tree, v.message))
            raise
    except subprocess.CalledProcessError as e:
        log("ceph osd tree command failed with message: {}".format(
            e.message))
        raise


def get_local_osd_ids():
    """
    This will list the /var/lib/ceph/osd/* directories and try
    to split the ID off of the directory name and return it in
    a list

    :return: list.  A list of osd identifiers :raise: OSError if
     something goes wrong with listing the directory.
    """
    osd_ids = []
    osd_path = os.path.join(os.sep, 'var', 'lib', 'ceph', 'osd')
    if os.path.exists(osd_path):
        try:
            dirs = os.listdir(osd_path)
            for osd_dir in dirs:
                osd_id = osd_dir.split('-')[1]
                if _is_int(osd_id):
                    osd_ids.append(osd_id)
        except OSError:
            raise
    return osd_ids


def get_local_mon_ids():
    """
    This will list the /var/lib/ceph/mon/* directories and try
    to split the ID off of the directory name and return it in
    a list

    :return: list.  A list of monitor identifiers :raise: OSError if
     something goes wrong with listing the directory.
    """
    mon_ids = []
    mon_path = os.path.join(os.sep, 'var', 'lib', 'ceph', 'mon')
    if os.path.exists(mon_path):
        try:
            dirs = os.listdir(mon_path)
            for mon_dir in dirs:
                # Basically this takes everything after ceph- as the monitor ID
                match = re.search('ceph-(?P<mon_id>.*)', mon_dir)
                if match:
                    mon_ids.append(match.group('mon_id'))
        except OSError:
            raise
    return mon_ids


def _is_int(v):
    """Return True if the object v can be turned into an integer."""
    try:
        int(v)
        return True
    except ValueError:
        return False


def get_version():
    """Derive Ceph release from an installed package."""
    import apt_pkg as apt

    cache = apt_cache()
    package = "ceph"
    try:
        pkg = cache[package]
    except:
        # the package is unknown to the current apt cache.
        e = 'Could not determine version of package with no installation ' \
            'candidate: %s' % package
        error_out(e)

    if not pkg.current_ver:
        # package is known, but no version is currently installed.
        e = 'Could not determine version of uninstalled package: %s' % package
        error_out(e)

    vers = apt.upstream_version(pkg.current_ver.ver_str)

    # x.y match only for 20XX.X
    # and ignore patch level for other packages
    match = re.match('^(\d+)\.(\d+)', vers)

    if match:
        vers = match.group(0)
    return float(vers)


def error_out(msg):
    log("FATAL ERROR: %s" % msg,
        level=ERROR)
    sys.exit(1)


def is_quorum():
    asok = "/var/run/ceph/ceph-mon.{}.asok".format(get_unit_hostname())
    cmd = [
        "sudo",
        "-u",
        ceph_user(),
        "ceph",
        "--admin-daemon",
        asok,
        "mon_status"
    ]
    if os.path.exists(asok):
        try:
            result = json.loads(subprocess.check_output(cmd))
        except subprocess.CalledProcessError:
            return False
        except ValueError:
            # Non JSON response from mon_status
            return False
        if result['state'] in QUORUM:
            return True
        else:
            return False
    else:
        return False


def is_leader():
    asok = "/var/run/ceph/ceph-mon.{}.asok".format(get_unit_hostname())
    cmd = [
        "sudo",
        "-u",
        ceph_user(),
        "ceph",
        "--admin-daemon",
        asok,
        "mon_status"
    ]
    if os.path.exists(asok):
        try:
            result = json.loads(subprocess.check_output(cmd))
        except subprocess.CalledProcessError:
            return False
        except ValueError:
            # Non JSON response from mon_status
            return False
        if result['state'] == LEADER:
            return True
        else:
            return False
    else:
        return False


def wait_for_quorum():
    while not is_quorum():
        log("Waiting for quorum to be reached")
        time.sleep(3)


def add_bootstrap_hint(peer):
    asok = "/var/run/ceph/ceph-mon.{}.asok".format(get_unit_hostname())
    cmd = [
        "sudo",
        "-u",
        ceph_user(),
        "ceph",
        "--admin-daemon",
        asok,
        "add_bootstrap_peer_hint",
        peer
    ]
    if os.path.exists(asok):
        # Ignore any errors for this call
        subprocess.call(cmd)


DISK_FORMATS = [
    'xfs',
    'ext4',
    'btrfs'
]

CEPH_PARTITIONS = [
    '4FBD7E29-9D25-41B8-AFD0-5EC00CEFF05D',  # ceph encrypted osd data
    '4FBD7E29-9D25-41B8-AFD0-062C0CEFF05D',  # ceph osd data
    '45B0969E-9B03-4F30-B4C6-B4B80CEFF106',  # ceph osd journal
]


def umount(mount_point):
    """
    This function unmounts a mounted directory forcibly.  This will
    be used for unmounting broken hard drive mounts which may hang.
    If umount returns EBUSY this will lazy unmount.
    :param mount_point: str.  A String representing the filesystem mount point
    :return: int.  Returns 0 on success.  errno otherwise.
    """
    libc_path = ctypes.util.find_library("c")
    libc = ctypes.CDLL(libc_path, use_errno=True)

    # First try to umount with MNT_FORCE
    ret = libc.umount(mount_point, 1)
    if ret < 0:
        err = ctypes.get_errno()
        if err == errno.EBUSY:
            # Detach from try.  IE lazy umount
            ret = libc.umount(mount_point, 2)
            if ret < 0:
                err = ctypes.get_errno()
                return err
            return 0
        else:
            return err
    return 0


def replace_osd(dead_osd_number,
                dead_osd_device,
                new_osd_device,
                osd_format,
                osd_journal,
                reformat_osd=False,
                ignore_errors=False):
    """
    This function will automate the replacement of a failed osd disk as much
    as possible. It will revoke the keys for the old osd, remove it from the
    crush map and then add a new osd into the cluster.
    :param dead_osd_number: The osd number found in ceph osd tree. Example: 99
    :param dead_osd_device: The physical device.  Example: /dev/sda
    :param osd_format:
    :param osd_journal:
    :param reformat_osd:
    :param ignore_errors:
    """
    host_mounts = mounts()
    mount_point = None
    for mount in host_mounts:
        if mount[1] == dead_osd_device:
            mount_point = mount[0]
    # need to convert dev to osd number
    # also need to get the mounted drive so we can tell the admin to
    # replace it
    try:
        # Drop this osd out of the cluster. This will begin a
        # rebalance operation
        status_set('maintenance', 'Removing osd {}'.format(dead_osd_number))
        subprocess.check_output([
            'ceph',
            '--id',
            'osd-upgrade',
            'osd', 'out',
            'osd.{}'.format(dead_osd_number)])

        # Kill the osd process if it's not already dead
        if systemd():
            service_stop('ceph-osd@{}'.format(dead_osd_number))
        else:
            subprocess.check_output(['stop', 'ceph-osd', 'id={}'.format(
                dead_osd_number)]),
        # umount if still mounted
        ret = umount(mount_point)
        if ret < 0:
            raise RuntimeError('umount {} failed with error: {}'.format(
                mount_point, os.strerror(ret)))
        # Clean up the old mount point
        shutil.rmtree(mount_point)
        subprocess.check_output([
            'ceph',
            '--id',
            'osd-upgrade',
            'osd', 'crush', 'remove',
            'osd.{}'.format(dead_osd_number)])
        # Revoke the OSDs access keys
        subprocess.check_output([
            'ceph',
            '--id',
            'osd-upgrade',
            'auth', 'del',
            'osd.{}'.format(dead_osd_number)])
        subprocess.check_output([
            'ceph',
            '--id',
            'osd-upgrade',
            'osd', 'rm',
            'osd.{}'.format(dead_osd_number)])
        status_set('maintenance', 'Setting up replacement osd {}'.format(
            new_osd_device))
        osdize(new_osd_device,
               osd_format,
               osd_journal,
               reformat_osd,
               ignore_errors)
    except subprocess.CalledProcessError as e:
        log('replace_osd failed with error: ' + e.output)


def is_osd_disk(dev):
    try:
        info = subprocess.check_output(['sgdisk', '-i', '1', dev])
        info = info.split("\n")  # IGNORE:E1103
        for line in info:
            for ptype in CEPH_PARTITIONS:
                sig = 'Partition GUID code: {}'.format(ptype)
                if line.startswith(sig):
                    return True
    except subprocess.CalledProcessError:
        pass
    return False


def start_osds(devices):
    # Scan for ceph block devices
    rescan_osd_devices()
    if cmp_pkgrevno('ceph', "0.56.6") >= 0:
        # Use ceph-disk activate for directory based OSD's
        for dev_or_path in devices:
            if os.path.exists(dev_or_path) and os.path.isdir(dev_or_path):
                subprocess.check_call(['ceph-disk', 'activate', dev_or_path])


def rescan_osd_devices():
    cmd = [
        'udevadm', 'trigger',
        '--subsystem-match=block', '--action=add'
    ]

    subprocess.call(cmd)


_bootstrap_keyring = "/var/lib/ceph/bootstrap-osd/ceph.keyring"
_upgrade_keyring = "/var/lib/ceph/osd/ceph.client.osd-upgrade.keyring"


def is_bootstrapped():
    return os.path.exists(_bootstrap_keyring)


def wait_for_bootstrap():
    while not is_bootstrapped():
        time.sleep(3)


def import_osd_bootstrap_key(key):
    if not os.path.exists(_bootstrap_keyring):
        cmd = [
            "sudo",
            "-u",
            ceph_user(),
            'ceph-authtool',
            _bootstrap_keyring,
            '--create-keyring',
            '--name=client.bootstrap-osd',
            '--add-key={}'.format(key)
        ]
        subprocess.check_call(cmd)


def import_osd_upgrade_key(key):
    if not os.path.exists(_upgrade_keyring):
        cmd = [
            "sudo",
            "-u",
            ceph_user(),
            'ceph-authtool',
            _upgrade_keyring,
            '--create-keyring',
            '--name=client.osd-upgrade',
            '--add-key={}'.format(key)
        ]
        subprocess.check_call(cmd)


def generate_monitor_secret():
    cmd = [
        'ceph-authtool',
        '/dev/stdout',
        '--name=mon.',
        '--gen-key'
    ]
    res = subprocess.check_output(cmd)

    return "{}==".format(res.split('=')[1].strip())

# OSD caps taken from ceph-create-keys
_osd_bootstrap_caps = {
    'mon': [
        'allow command osd create ...',
        'allow command osd crush set ...',
        r'allow command auth add * osd allow\ * mon allow\ rwx',
        'allow command mon getmap'
    ]
}

_osd_bootstrap_caps_profile = {
    'mon': [
        'allow profile bootstrap-osd'
    ]
}


def parse_key(raw_key):
    # get-or-create appears to have different output depending
    # on whether its 'get' or 'create'
    # 'create' just returns the key, 'get' is more verbose and
    # needs parsing
    key = None
    if len(raw_key.splitlines()) == 1:
        key = raw_key
    else:
        for element in raw_key.splitlines():
            if 'key' in element:
                key = element.split(' = ')[1].strip()  # IGNORE:E1103
    return key


def get_osd_bootstrap_key():
    try:
        # Attempt to get/create a key using the OSD bootstrap profile first
        key = get_named_key('bootstrap-osd',
                            _osd_bootstrap_caps_profile)
    except:
        # If that fails try with the older style permissions
        key = get_named_key('bootstrap-osd',
                            _osd_bootstrap_caps)
    return key


_radosgw_keyring = "/etc/ceph/keyring.rados.gateway"


def import_radosgw_key(key):
    if not os.path.exists(_radosgw_keyring):
        cmd = [
            "sudo",
            "-u",
            ceph_user(),
            'ceph-authtool',
            _radosgw_keyring,
            '--create-keyring',
            '--name=client.radosgw.gateway',
            '--add-key={}'.format(key)
        ]
        subprocess.check_call(cmd)

# OSD caps taken from ceph-create-keys
_radosgw_caps = {
    'mon': ['allow rw'],
    'osd': ['allow rwx']
}
_upgrade_caps = {
    'mon': ['allow rwx']
}


def get_radosgw_key():
    return get_named_key('radosgw.gateway', _radosgw_caps)


_default_caps = {
    'mon': ['allow rw'],
    'osd': ['allow rwx']
}

admin_caps = {
    'mds': ['allow *'],
    'mon': ['allow *'],
    'osd': ['allow *']
}

osd_upgrade_caps = {
    'mon': ['allow command "config-key"',
            'allow command "osd tree"',
            'allow command "config-key list"',
            'allow command "config-key put"',
            'allow command "config-key get"',
            'allow command "config-key exists"',
            'allow command "osd out"',
            'allow command "osd in"',
            'allow command "osd rm"',
            'allow command "auth del"',
            ]
}


def get_upgrade_key():
    return get_named_key('upgrade-osd', _upgrade_caps)


def get_named_key(name, caps=None):
    caps = caps or _default_caps
    cmd = [
        "sudo",
        "-u",
        ceph_user(),
        'ceph',
        '--name', 'mon.',
        '--keyring',
        '/var/lib/ceph/mon/ceph-{}/keyring'.format(
            get_unit_hostname()
        ),
        'auth', 'get-or-create', 'client.{}'.format(name),
    ]
    # Add capabilities
    for subsystem, subcaps in caps.iteritems():
        cmd.extend([
            subsystem,
            '; '.join(subcaps),
        ])
    return parse_key(subprocess.check_output(cmd).strip())  # IGNORE:E1103


def upgrade_key_caps(key, caps):
    """ Upgrade key to have capabilities caps """
    if not is_leader():
        # Not the MON leader OR not clustered
        return
    cmd = [
        "sudo", "-u", ceph_user(), 'ceph', 'auth', 'caps', key
    ]
    for subsystem, subcaps in caps.iteritems():
        cmd.extend([subsystem, '; '.join(subcaps)])
    subprocess.check_call(cmd)


@cached
def systemd():
    return (lsb_release()['DISTRIB_CODENAME'] >= 'vivid')


def bootstrap_monitor_cluster(secret):
    hostname = get_unit_hostname()
    path = '/var/lib/ceph/mon/ceph-{}'.format(hostname)
    done = '{}/done'.format(path)
    if systemd():
        init_marker = '{}/systemd'.format(path)
    else:
        init_marker = '{}/upstart'.format(path)

    keyring = '/var/lib/ceph/tmp/{}.mon.keyring'.format(hostname)

    if os.path.exists(done):
        log('bootstrap_monitor_cluster: mon already initialized.')
    else:
        # Ceph >= 0.61.3 needs this for ceph-mon fs creation
        mkdir('/var/run/ceph', owner=ceph_user(),
              group=ceph_user(), perms=0o755)
        mkdir(path, owner=ceph_user(), group=ceph_user())
        # end changes for Ceph >= 0.61.3
        try:
            subprocess.check_call(['ceph-authtool', keyring,
                                   '--create-keyring', '--name=mon.',
                                   '--add-key={}'.format(secret),
                                   '--cap', 'mon', 'allow *'])

            subprocess.check_call(['ceph-mon', '--mkfs',
                                   '-i', hostname,
                                   '--keyring', keyring])
            chownr(path, ceph_user(), ceph_user())
            with open(done, 'w'):
                pass
            with open(init_marker, 'w'):
                pass

            if systemd():
                subprocess.check_call(['systemctl', 'enable', 'ceph-mon'])
                service_restart('ceph-mon')
            else:
                service_restart('ceph-mon-all')
        except:
            raise
        finally:
            os.unlink(keyring)


def update_monfs():
    hostname = get_unit_hostname()
    monfs = '/var/lib/ceph/mon/ceph-{}'.format(hostname)
    if systemd():
        init_marker = '{}/systemd'.format(monfs)
    else:
        init_marker = '{}/upstart'.format(monfs)
    if os.path.exists(monfs) and not os.path.exists(init_marker):
        # Mark mon as managed by upstart so that
        # it gets start correctly on reboots
        with open(init_marker, 'w'):
            pass


def maybe_zap_journal(journal_dev):
    if is_osd_disk(journal_dev):
        log('Looks like {} is already an OSD data'
            ' or journal, skipping.'.format(journal_dev))
        return
    zap_disk(journal_dev)
    log("Zapped journal device {}".format(journal_dev))


def get_partitions(dev):
    cmd = ['partx', '--raw', '--noheadings', dev]
    try:
        out = subprocess.check_output(cmd).splitlines()
        log("get partitions: {}".format(out), level=DEBUG)
        return out
    except subprocess.CalledProcessError as e:
        log("Can't get info for {0}: {1}".format(dev, e.output))
        return []


def find_least_used_journal(journal_devices):
    usages = map(lambda a: (len(get_partitions(a)), a), journal_devices)
    least = min(usages, key=lambda t: t[0])
    return least[1]


def osdize(dev, osd_format, osd_journal, reformat_osd=False,
           ignore_errors=False, encrypt=False):
    if dev.startswith('/dev'):
        osdize_dev(dev, osd_format, osd_journal,
                   reformat_osd, ignore_errors, encrypt)
    else:
        osdize_dir(dev, encrypt)


def osdize_dev(dev, osd_format, osd_journal, reformat_osd=False,
               ignore_errors=False, encrypt=False):
    if not os.path.exists(dev):
        log('Path {} does not exist - bailing'.format(dev))
        return

    if not is_block_device(dev):
        log('Path {} is not a block device - bailing'.format(dev))
        return

    if is_osd_disk(dev) and not reformat_osd:
        log('Looks like {} is already an'
            ' OSD data or journal, skipping.'.format(dev))
        return

    if is_device_mounted(dev):
        log('Looks like {} is in use, skipping.'.format(dev))
        return

    status_set('maintenance', 'Initializing device {}'.format(dev))
    cmd = ['ceph-disk', 'prepare']
    # Later versions of ceph support more options
    if cmp_pkgrevno('ceph', '0.60') >= 0:
        if encrypt:
            cmd.append('--dmcrypt')
    if cmp_pkgrevno('ceph', '0.48.3') >= 0:
        if osd_format:
            cmd.append('--fs-type')
            cmd.append(osd_format)
        if reformat_osd:
            cmd.append('--zap-disk')
        cmd.append(dev)
        if osd_journal:
            least_used = find_least_used_journal(osd_journal)
            cmd.append(least_used)
    else:
        # Just provide the device - no other options
        # for older versions of ceph
        cmd.append(dev)
        if reformat_osd:
            zap_disk(dev)

    try:
        log("osdize cmd: {}".format(cmd))
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as e:
        if ignore_errors:
            log('Unable to initialize device: {}'.format(dev), WARNING)
        else:
            log('Unable to initialize device: {}'.format(dev), ERROR)
            raise e


def osdize_dir(path, encrypt=False):
    if os.path.exists(os.path.join(path, 'upstart')):
        log('Path {} is already configured as an OSD - bailing'.format(path))
        return

    if cmp_pkgrevno('ceph', "0.56.6") < 0:
        log('Unable to use directories for OSDs with ceph < 0.56.6',
            level=ERROR)
        raise

    mkdir(path, owner=ceph_user(), group=ceph_user(), perms=0o755)
    chownr('/var/lib/ceph', ceph_user(), ceph_user())
    cmd = [
        'sudo', '-u', ceph_user(),
        'ceph-disk',
        'prepare',
        '--data-dir',
        path
    ]
    if cmp_pkgrevno('ceph', '0.60') >= 0:
        if encrypt:
            cmd.append('--dmcrypt')
    log("osdize dir cmd: {}".format(cmd))
    subprocess.check_call(cmd)


def filesystem_mounted(fs):
    return subprocess.call(['grep', '-wqs', fs, '/proc/mounts']) == 0


def get_running_osds():
    """Returns a list of the pids of the current running OSD daemons"""
    cmd = ['pgrep', 'ceph-osd']
    try:
        result = subprocess.check_output(cmd)
        return result.split()
    except subprocess.CalledProcessError:
        return []