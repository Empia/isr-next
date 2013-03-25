#
# vmnetx.generate - Generation of a vmnetx machine image
#
# Copyright (C) 2012-2013 Carnegie Mellon University
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

from __future__ import division
import os
import struct
import subprocess
import sys
from tempfile import NamedTemporaryFile

from vmnetx.domain import DomainXML, DomainXMLError
from vmnetx.package import Package
from vmnetx.util import DetailException

class MachineGenerationError(DetailException):
    pass


# File handle arguments don't need more than two letters
# pylint: disable=C0103
class _QemuMemoryHeader(object):
    HEADER_MAGIC = 'LibvirtQemudSave'
    HEADER_VERSION = 2
    # Header values are stored "native-endian".  We only support x86, so
    # assume we don't need to byteswap.
    HEADER_FORMAT = str(len(HEADER_MAGIC)) + 's19I'
    HEADER_LENGTH = struct.calcsize(HEADER_FORMAT)
    HEADER_UNUSED_VALUES = 15

    COMPRESS_RAW = 0
    COMPRESS_XZ = 3

    # pylint is confused by "\0", #111799
    # pylint: disable=W1401
    def __init__(self, f):
        # Read header struct
        f.seek(0)
        buf = f.read(self.HEADER_LENGTH)
        header = list(struct.unpack(self.HEADER_FORMAT, buf))
        magic = header.pop(0)
        version = header.pop(0)
        self._xml_len = header.pop(0)
        self.was_running = header.pop(0)
        self.compressed = header.pop(0)

        # Check header
        if magic != self.HEADER_MAGIC:
            raise MachineGenerationError('Invalid memory image magic')
        if version != self.HEADER_VERSION:
            raise MachineGenerationError('Unknown memory image version %d' %
                    version)
        if header != [0] * self.HEADER_UNUSED_VALUES:
            raise MachineGenerationError('Unused header values not 0')

        # Read XML, drop trailing NUL padding
        self.xml = f.read(self._xml_len - 1).rstrip('\0')
        if f.read(1) != '\0':
            raise MachineGenerationError('Missing NUL byte after XML')
    # pylint: enable=W1401

    def seek_body(self, f):
        f.seek(self.HEADER_LENGTH + self._xml_len)

    def write(self, f):
        # Calculate header
        if len(self.xml) > self._xml_len - 1:
            # If this becomes a problem, we could write out a larger xml_len,
            # though this must be page-aligned.
            raise MachineGenerationError('self.xml is too large')
        header = [self.HEADER_MAGIC,
                self.HEADER_VERSION,
                self._xml_len,
                self.was_running,
                self.compressed]
        header.extend([0] * self.HEADER_UNUSED_VALUES)

        # Write data
        f.seek(0)
        f.write(struct.pack(self.HEADER_FORMAT, *header))
        f.write(struct.pack('%ds' % self._xml_len, self.xml))
# pylint: enable=C0103


def copy_memory(in_path, out_path, xml=None, compress=True):
    # Ensure the input is uncompressed, even if we will not be compressing
    fin = open(in_path)
    fout = open(out_path, 'w')
    hdr = _QemuMemoryHeader(fin)
    if hdr.compressed != hdr.COMPRESS_RAW:
        raise MachineGenerationError('Cannot recompress save format %d' %
                hdr.compressed)

    # Write header
    if compress:
        hdr.compressed = hdr.COMPRESS_XZ
    if xml is not None:
        hdr.xml = xml
    hdr.write(fout)

    # Print size of uncompressed image
    fin.seek(0, 2)
    total = fin.tell()
    hdr.seek_body(fin)
    if compress:
        action = 'Copying and compressing'
    else:
        action = 'Copying'
    print '%s memory image (%d MB)...' % (action, (total - fin.tell()) >> 20)

    # Write body
    fout.flush()
    if compress:
        ret = subprocess.call(['xz', '-9cv'], stdin=fin, stdout=fout)
        if ret:
            raise IOError('XZ compressor failed')
    else:
        while True:
            buf = fin.read(1 << 20)
            if not buf:
                break
            fout.write(buf)
            print '  %3d%%\r' % (100 * fout.tell() / total),
            sys.stdout.flush()
        print


def copy_disk(in_path, type, out_path, raw=False):
    if raw:
        print 'Copying disk image...'
        ret = subprocess.call(['qemu-img', 'convert', '-p', '-f', type,
                '-O', 'raw', in_path, out_path])
    else:
        print 'Copying and compressing disk image...'
        ret = subprocess.call(['qemu-img', 'convert', '-cp', '-f', type,
                '-O', 'qcow2', in_path, out_path])

    if ret != 0:
        raise MachineGenerationError('qemu-img failed')


def generate_machine(name, in_xml, out_file, compress=True):
    # Parse domain XML
    try:
        with open(in_xml) as fh:
            domain = DomainXML(fh.read(), strict=True)
    except (IOError, DomainXMLError), e:
        raise MachineGenerationError(str(e), getattr(e, 'detail', None))

    # Get memory path
    in_memory = os.path.join(os.path.dirname(in_xml), 'save',
            '%s.save' % os.path.splitext(os.path.basename(in_xml))[0])

    # Generate domain XML
    domain_xml = domain.get_for_storage(disk_type='qcow2' if compress
            else 'raw').xml

    temp_disk = None
    temp_memory = None
    try:
        # Copy disk
        out_dir = os.path.dirname(out_file)
        temp_disk = NamedTemporaryFile(dir=out_dir, prefix='disk-')
        copy_disk(domain.disk_path, domain.disk_type, temp_disk.name,
                raw=not compress)

        # Copy memory
        if os.path.exists(in_memory):
            temp_memory = NamedTemporaryFile(dir=out_dir, prefix='memory-')
            copy_memory(in_memory, temp_memory.name, domain_xml,
                    compress=compress)
        else:
            print 'No memory image found'

        # Write package
        print 'Writing package...'
        try:
            Package.create(out_file, name, domain_xml, temp_disk.name,
                    temp_memory.name if temp_memory else None)
        except:
            os.unlink(out_file)
            raise
    finally:
        if temp_disk:
            temp_disk.close()
        if temp_memory:
            temp_memory.close()
