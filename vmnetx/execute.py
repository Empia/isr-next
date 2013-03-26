#
# vmnetx.execute - Execution of a virtual machine
#
# Copyright (C) 2011-2013 Carnegie Mellon University
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of version 2 of the GNU General Public License as published
# by the Free Software Foundation.  A copy of the GNU General Public License
# should have been distributed along with this program in the file
# COPYING.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#

# pylint doesn't understand hashlib.sha256
# pylint: disable=E0611
from hashlib import sha256
# pylint: enable=E0611
import libvirt
import os
import tempfile
from urlparse import urlsplit, urlunsplit
import uuid

from vmnetx.package import Package
from vmnetx.util import ensure_dir

try:
    from selinux import chcon
except ImportError:
    def chcon(*_args, **_kwargs):
        pass

from vmnetx.domain import DomainXML
from vmnetx.util import get_cache_dir, get_temp_dir
from vmnetx.vmnetfs import VMNetFS

SOCKET_DIR_CONTEXT = 'unconfined_u:object_r:virt_home_t:s0'

class MachineExecutionError(Exception):
    pass


class _ReferencedObject(object):
    # pylint doesn't understand named tuples
    # pylint: disable=E1103
    def __init__(self, label, info, chunk_size=131072):
        self.url = info.url
        self.offset = info.offset
        self.size = info.size
        self.chunk_size = chunk_size

        basepath = os.path.join(get_cache_dir(), 'chunks')
        # Exclude query string from cache path
        parsed_url = urlsplit(self.url)
        self._cache_url = urlunsplit((parsed_url.scheme, parsed_url.netloc,
                parsed_url.path, '', ''))
        self._urlpath = os.path.join(basepath,
                sha256(self._cache_url).hexdigest())
        # Hash collisions will allow cache poisoning!
        self.cache = os.path.join(self._urlpath, label, str(chunk_size))
    # pylint: enable=E1103

    @property
    def vmnetfs_args(self):
        # Write URL into file for ease of debugging.  Defer creation of
        # cache directory until needed.
        ensure_dir(self._urlpath)
        urlfile = os.path.join(self._urlpath, 'url')
        if not os.path.exists(urlfile):
            with open(urlfile, 'w') as fh:
                fh.write('%s\n' % self._cache_url)
        return [self.url, self.cache, str(self.offset), str(self.size),
                str(self.chunk_size)]


class MachineMetadata(object):
    # pylint doesn't understand named tuples
    # pylint: disable=E1103
    def __init__(self, package_ref, scheme=None, username=None, password=None):
        # Convert package_ref to package URL
        url = package_ref
        parsed = urlsplit(url)
        if parsed.scheme == '':
            # Local file path
            url = urlunsplit(('file', '', os.path.abspath(parsed.path),
                    '', ''))

        # Load package
        self.package = Package(url, scheme=scheme, username=username,
                password=password)

        # Validate domain XML
        self.domain_xml = DomainXML(self.package.domain.data)

        # Create vmnetfs arguments
        self.vmnetfs_args = [username or '', password or ''] + \
                _ReferencedObject('disk', self.package.disk).vmnetfs_args
        if self.package.memory:
            self.vmnetfs_args.extend(_ReferencedObject('memory',
                    self.package.memory).vmnetfs_args)
    # pylint: enable=E1103


class Machine(object):
    def __init__(self, metadata):
        self.name = metadata.package.name
        self._domain_name = 'vmnetx-%d-%s' % (os.getpid(), uuid.uuid4())
        self._vnc_socket_dir = tempfile.mkdtemp(dir=get_temp_dir(),
                prefix='vmnetx-socket-')
        self.vnc_listen_address = os.path.join(self._vnc_socket_dir, 'vnc')

        # Fix socket dir SELinux context
        try:
            chcon(self._vnc_socket_dir, SOCKET_DIR_CONTEXT)
        except OSError:
            pass

        # Start vmnetfs
        self._fs = VMNetFS(metadata.vmnetfs_args)
        self._fs.start()
        self.disk_path = os.path.join(self._fs.mountpoint, 'disk')
        disk_image_path = os.path.join(self.disk_path, 'image')
        if metadata.package.memory:
            self.memory_path = os.path.join(self._fs.mountpoint, 'memory')
            self._memory_image_path = os.path.join(self.memory_path, 'image')
        else:
            self.memory_path = self._memory_image_path = None

        # Set up libvirt connection
        self._conn = libvirt.open('qemu:///session')

        # Get execution domain XML
        self._domain_xml = metadata.domain_xml.get_for_execution(
                self._conn, self._domain_name, disk_image_path,
                self.vnc_listen_address).xml

    def start_vm(self, cold=False):
        try:
            if not cold and self._memory_image_path is not None:
                # Does not return domain handle
                # Does not allow autodestroy
                self._conn.restoreFlags(self._memory_image_path,
                        self._domain_xml, libvirt.VIR_DOMAIN_SAVE_RUNNING)
            else:
                self._conn.createXML(self._domain_xml,
                        libvirt.VIR_DOMAIN_START_AUTODESTROY)
        except libvirt.libvirtError, e:
            raise MachineExecutionError(str(e))

    def stop_vm(self):
        try:
            self._conn.lookupByName(self._domain_name).destroy()
        except libvirt.libvirtError:
            # Assume that the VM did not exist or was already dying
            pass

    def close(self):
        # Close libvirt connection
        self._conn.close()

        # Delete VNC socket
        try:
            os.unlink(self.vnc_listen_address)
        except OSError:
            pass
        try:
            os.rmdir(self._vnc_socket_dir)
        except OSError:
            pass

        # Terminate vmnetfs
        self._fs.terminate()
