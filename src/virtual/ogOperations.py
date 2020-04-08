#
# Copyright (C) 2020 Soleta Networks <info@soleta.eu>
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the
# Free Software Foundation, version 3.
#

import socket
import errno
import enum
import json
import subprocess
import shutil
import os
import guestfs
import hivex
import pathlib
import re
import math

class OgQMP:
    class State(enum.Enum):
        CONNECTING = 0
        RECEIVING = 1
        FORCE_DISCONNECTED = 2

    def __init__(self, ip, port):
        self.ip = ip
        self.port = port

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.state = self.State.CONNECTING
        self.data = ""

        try:
            self.sock.connect((self.ip, self.port))
        except socket.error as err:
            print('Error connection' + str(err))
            return None

    def recv(self):
        self.data = self.sock.recv(50024).decode('utf-8')
        return self.data


    def send(self, data=None):
        if not data:
            return None

        self.sock.send(bytes(data, 'utf-8'))
        return len(data)

    def disconnect(self):
        self.state = self.State.FORCE_DISCONNECTED
        self.sock.close()

class OgVirtualOperations:
    def __init__(self):
        self.IP = '127.0.0.1'
        self.VIRTUAL_PORT = 4444
        self.USABLE_DISK = 0.75

    def poweroff(self):
        qmp = OgQMP(self.IP, self.VIRTUAL_PORT)
        qmp.connect()
        qmp.recv()
        qmp.send(str({"execute": "qmp_capabilities"}))
        qmp.recv()
        qmp.send(str({"execute": "system_powerdown"}))
        qmp.disconnect()

    def reboot(self):
        qmp = OgQMP(self.IP, self.VIRTUAL_PORT)
        qmp.connect()
        qmp.recv()
        qmp.send(str({"execute": "qmp_capabilities"}))
        qmp.recv()
        qmp.send(str({"execute": "system_reset"}))
        qmp.disconnect()

    def session(self, request, ogRest):
        disk = request.getDisk()
        partition = request.getPartition()

        available_ram = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
        available_ram_mib = available_ram / 1024 ** 2
        # Calculate the lower power of 2 amout of RAM memory for the VM.
        vm_ram_mib = 2 ** math.floor(math.log(available_ram_mib) / math.log(2))

        cmd = (f'qemu-system-x86_64 -hda disk{disk}_part{partition}.qcow2 '
               f'-qmp tcp:localhost:4444,server,nowait --enable-kvm '
               f'-display gtk -cpu host -m {vm_ram_mib}M -boot c')
        subprocess.Popen([cmd], shell=True)

    def refresh(self, ogRest):
        path = 'partitions.json'
        temp_mount_dir = 'mnt'
        try:
            with open(path, 'r+') as f:
                data = json.loads(f.read())
                for part in data['partition_setup']:
                    if len(part['virt-drive']) > 0:
                        subprocess.run([f'qemu-nbd -c /dev/nbd0 {part["virt-drive"]}'],
                                       shell=True)
                        subprocess.run([f'partprobe /dev/nbd0'],
                                       shell=True)
                        subprocess.run([f'mount /dev/nbd0p1 {temp_mount_dir}'],
                                       shell=True)
                        total_disk, used_disk, free_disk = shutil.disk_usage(temp_mount_dir)
                        free_disk = int(free_disk * self.USABLE_DISK)
                        subprocess.run([f'umount {temp_mount_dir}'],
                                       shell=True)
                        subprocess.run(['qemu-nbd -d /dev/nbd0'],
                                       shell=True)
                        part['size'] = int(free_disk / 1024)
                        part['used_size'] = int(100 * used_disk / free_disk)
                f.seek(0)
                f.write(json.dumps(data, indent=4))
                f.truncate()
        except FileNotFoundError:
            total_disk, used_disk, free_disk = shutil.disk_usage("/")
            free_disk = int(free_disk * self.USABLE_DISK)
            data = {'serial_number': '',
                    'disk_setup': {'disk': 1,
                                   'partition': 0,
                                   'code': '0',
                                   'filesystem': '',
                                   'os': '',
                                   'size': int(free_disk / 1024),
                                   'used_size': int(100 * used_disk / free_disk)},
                    'partition_setup': []}
            for i in range(4):
                part_json = {'disk': 1,
                             'partition': i + 1,
                             'code': '',
                             'filesystem': 'EMPTY',
                             'os': '',
                             'size': 0,
                             'used_size': 0,
                             'virt-drive': ''}
                data['partition_setup'].append(part_json)
            with open(path, 'w+') as f:
                f.write(json.dumps(data, indent=4))

        # TODO no debería ser necesario eliminar virt-drive ni transformar a strings
        for part in data['partition_setup']:
            part.pop('virt-drive')
            for k, v in part.items():
                part[k] = str(v)
        data['disk_setup'] = {k: str(v) for k, v in data['disk_setup'].items()}

        return data

    def setup(self, request, ogRest):
        path = 'partitions.json'
        refresh(ogRest)

        part_setup = request.getPartitionSetup()
        disk = request.getDisk()

        for i, part in enumerate(part_setup):
            if int(part['format']) == 0:
                continue

            drive_path = f'disk{disk}_part{part["partition"]}.qcow2'
            sequence = [f'qemu-img create -f qcow2 {drive_path} {part["size"]}K',
                        f'qemu-nbd -c /dev/nbd0 {drive_path}',
                        f'parted /dev/nbd0 mklabel gpt',
                        f'parted -a opt /dev/nbd0 mkpart primary {part["filesystem"]} 2048s 100%',
                        f'mkfs.{part["filesystem"].lower()} /dev/nbd0p1',
                        f'qemu-nbd -d /dev/nbd0']
            for cmd in sequence:
                process = subprocess.run([cmd], shell=True)
                if process.returncode != 0:
                    raise RuntimeError

            with open(path, 'r+') as f:
                data = json.loads(f.read())
                if part['code'] == 'LINUX':
                    data['partition_setup'][i]['code'] = '0083'
                elif part['code'] == 'NTFS':
                    data['partition_setup'][i]['code'] = '0007'
                elif part['code'] == 'DATA':
                    data['partition_setup'][i]['code'] = '00da'
                data['partition_setup'][i]['filesystem'] = part['filesystem']
                data['partition_setup'][i]['size'] = int(part['size'])
                data['partition_setup'][i]['virt-drive'] = drive_path
                f.seek(0)
                f.write(json.dumps(data, indent=4))
                f.truncate()

        return refresh(ogRest)

    def image_create(self, path, request, ogRest):
        disk = request.getDisk()
        partition = request.getPartition()
        name = request.getName()
        repo = request.getRepo()

        # Check if VM is running.
        qmp = OgQMP(self.IP, self.VIRTUAL_PORT)
        if qmp.connect() != None:
            qmp.disconnect()
            return None

        refresh(ogRest)

        drive_path = f'disk{disk}_part{partition}.qcow2'
        repo_path = f'images'

        try:
            shutil.copy(drive_path, f'{repo_path}/{name}')
        except:
            return None

        return True

    def image_restore(self, request, ogRest):
        disk = request.getDisk()
        partition = request.getPartition()
        name = request.getName()
        repo = request.getRepo()
        # TODO Multicast? Unicast? Solo copia con samba.
        ctype = request.getType()
        profile = request.getProfile()
        cid = request.getId()

        # Check if VM is running.
        qmp = OgQMP(self.IP, self.VIRTUAL_PORT)
        if qmp.connect() != None:
            qmp.disconnect()
            return None

        refresh(ogRest)

        drive_path = f'disk{disk}_part{partition}.qcow2'
        repo_path = f'images'

        if os.path.exists(drive_path):
            os.remove(drive_path)

        try:
            shutil.copy(f'{repo_path}/{name}', drive_path)
        except:
            return None

        return True

    def software(self, request, path, ogRest):
        DPKG_PATH = '/var/lib/dpkg/status'

        disk = request.getDisk()
        partition = request.getPartition()
        drive_path = f'disk{disk}_part{partition}.qcow2'
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(drive_path, readonly=1)
        g.launch()
        root = g.inspect_os()[0]

        os_type = g.inspect_get_type(root)
        os_major_version = g.inspect_get_major_version(root)
        os_minor_version = g.inspect_get_minor_version(root)
        os_distro = g.inspect_get_distro(root)

        software = []

        if 'linux' in os_type:
            g.mount_ro(g.list_partitions()[0], '/')
            try:
                g.download('/' + DPKG_PATH, 'dpkg_list')
            except:
                pass
            g.umount_all()
            if os.path.isfile('dpkg_list'):
                pkg_pattern = re.compile('Package: (.+)')
                version_pattern = re.compile('Version: (.+)')
                with open('dpkg_list', 'r') as f:
                    for line in f:
                        pkg_match = pkg_pattern.match(line)
                        version_match = version_pattern.match(line)
                        if pkg_match:
                            pkg = pkg_match.group(1)
                        elif version_match:
                            version = version_match.group(1)
                        elif line == '\n':
                            software.append(pkg + ' ' + version)
                        else:
                            continue
                os.remove('dpkg_list')
        elif 'windows' in os_type:
            g.mount_ro(g.list_partitions()[0], '/')
            hive_file_path = g.inspect_get_windows_software_hive(root)
            g.download('/' + hive_file_path, 'win_reg')
            g.umount_all()
            h = hivex.Hivex('win_reg')
            key = h.root()
            key = h.node_get_child (key, 'Microsoft')
            key = h.node_get_child (key, 'Windows')
            key = h.node_get_child (key, 'CurrentVersion')
            key = h.node_get_child (key, 'Uninstall')
            software += [h.node_name(x) for x in h.node_children(key)]
            # Just for 64 bit Windows versions, check for 32 bit software.
            if (os_major_version == 5 and os_minor_version >= 2) or \
               (os_major_version >= 6):
                key = h.root()
                key = h.node_get_child (key, 'Wow6432Node')
                key = h.node_get_child (key, 'Microsoft')
                key = h.node_get_child (key, 'Windows')
                key = h.node_get_child (key, 'CurrentVersion')
                key = h.node_get_child (key, 'Uninstall')
                software += [h.node_name(x) for x in h.node_children(key)]
            os.remove('win_reg')

        with open(path, 'w+') as f:
            f.seek(0)
            for program in software:
                f.write(f'{program}\n')
            f.truncate()

    def parse_pci(self, path='/usr/share/misc/pci.ids'):
        data = {}
        with open(path, 'r') as f:
            for line in f:
                if line[0] == '#':
                    continue
                elif len(line.strip()) == 0:
                    continue
                else:
                    if line[:2] == '\t\t':
                        fields = line.strip().split(maxsplit=2)
                        data[last_vendor][last_device][(fields[0], fields[1])] = fields[2]
                    elif line[:1] == '\t':
                        fields = line.strip().split(maxsplit=1)
                        last_device = fields[0]
                        data[last_vendor][fields[0]] = {'name': fields[1]}
                    else:
                        fields = line.strip().split(maxsplit=1)
                        if fields[0] == 'ffff':
                            break
                        last_vendor = fields[0]
                        data[fields[0]] = {'name': fields[1]}
        return data

    def hardware(self, path, ogRest):
        qmp = OgQMP(self.IP, self.VIRTUAL_PORT)
        qmp.connect()
        qmp.recv()
        qmp.send(str({"execute": "qmp_capabilities"}))
        qmp.recv()
        qmp.send(str({"execute": "query-pci"}))
        data = json.loads(qmp.recv())
        data = data['return'][0]['devices']
        pci_list = parse_pci()
        device_names = {}
        for device in data:
            vendor_id = hex(device['id']['vendor'])[2:]
            device_id = hex(device['id']['device'])[2:]
            subvendor_id = hex(device['id']['subsystem-vendor'])[2:]
            subdevice_id = hex(device['id']['subsystem'])[2:]
            description = device['class_info']['desc'].lower()
            name = ''
            try:
                name = pci_list[vendor_id]['name']
                name += ' ' + pci_list[vendor_id][device_id]['name']
                name += ' ' + pci_list[vendor_id][device_id][(subvendor_id, subdevice_id)]
            except KeyError:
                if vendor_id == '1234':
                    name = 'VGA Cirrus Logic GD 5446'
                else:
                    pass

            if 'usb' in description:
                device_names['usb'] = name
            elif 'ide' in description:
                device_names['ide'] = name
            elif 'ethernet' in description:
                device_names['net'] = name
            elif 'vga' in description:
                device_names['vga'] = name

            elif 'audio' in description or 'sound' in description:
                device_names['aud'] = name
            elif 'dvd' in description:
                device_names['cdr'] = name

        qmp.send(str({"execute": "query-memory-size-summary"}))
        data = json.loads(qmp.recv())
        ram_size = int(data['return']['base-memory']) * 2 ** -20
        device_names['mem'] = f'QEMU {int(ram_size)}MiB'

        qmp.send(str({"execute": "query-cpus-fast"}))
        data = json.loads(qmp.recv())
        qmp.disconnect()
        cpu_arch = data['return'][0]['arch']
        cpu_target = data['return'][0]['target']
        cpu_cores = len(data['return'])
        device_names['cpu'] = f'CPU arch:{cpu_arch} target:{cpu_target} ' \
                              f'cores:{cpu_cores}'

        with open(path, 'w+') as f:
            f.seek(0)
            for k, v in device_names.items():
                f.write(f'{k}={v}\n')
            f.truncate()