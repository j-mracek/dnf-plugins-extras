#
# Copyright (C) 2015  Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#

from __future__ import absolute_import
from __future__ import unicode_literals
from dnfpluginsextras import _, logger

import dnf
import dnf.cli
import dnf.exceptions
import fnmatch
import hawkey
import os
import tempfile
import time

NOT_READABLE = _('Unable to read version lock configuration: %s')
NO_LOCKLIST = _('Locklist not set')
ADDING_SPEC = _('Adding versionlock on:')
EXCLUDING_SPEC = _('Adding exclude on:')
DELETING_SPEC = _('Deleting versionlock for:')
NOTFOUND_SPEC = _('No package found for:')

locklist_fn = None

class VersionLock(dnf.Plugin):

    name = 'versionlock'

    def __init__(self, base, cli):
        super(VersionLock, self).__init__(base, cli)
        self.base = base
        self.cli = cli
        if self.cli is not None:
            self.cli.register_command(VersionLockCommand)

    def config(self):
        global locklist_fn
        cp = self.read_config(self.base.conf)
        locklist_fn = cp.get('main','locklist')

    def sack(self):
        if not locklist_fn:
            raise dnf.exceptions.Error(NO_LOCKLIST)

        locked = set()
        for pat in _read_locklist():
            excl = False
            if pat and pat[0] == '!':
                pat = pat[1:]
                excl = True

            subj = dnf.subject.Subject(pat)
            pkgs = subj.get_best_query(self.base.sack)

            if excl:
                self.base.sack.add_excludes(pkgs)
            else:
                locked.update(pkgs.run())

        if locked:
            locked_names = [pkg.name for pkg in locked]
            all_versions = set(self.base.sack.query().filter(name=locked_names))
            other_versions = all_versions.difference(locked)
            self.base.sack.add_excludes(other_versions)

EXC_CMDS = ['exclude', 'add-!', 'add!', 'blacklist']
DEL_CMDS = ['delete', 'del']
ALL_CMDS = ['add', 'clear', 'list'] + EXC_CMDS + DEL_CMDS

class VersionLockCommand(dnf.cli.Command):

    aliases = ("versionlock",)
    summary = _("control package version locks")
    usage = "[add|exclude|list|delete|clear] [<package-nevr-spec>]"

    @staticmethod
    def set_argparser(parser):
        parser.add_argument("subcommand", nargs='?',
                            metavar="[add|exclude|list|delete|clear]")
        parser.add_argument("package", nargs='*',
                            metavar="[<package-nevr-spec>]")


    def configure(self):
        self.cli.demands.sack_activation = True
        self.cli.demands.available_repos = True

    def run(self):
        cmd = 'list'
        if self.opts.subcommand:
            if self.opts.subcommand not in ALL_CMDS:
                cmd = 'add'
                self.opts.package.insert(0, self.opts.subcommand)
            elif self.opts.subcommand in EXC_CMDS:
                cmd = 'exclude'
            elif self.opts.subcommand in DEL_CMDS:
                cmd = 'delete'
            else:
                cmd = self.opts.subcommand

        if cmd == 'add':
            _write_locklist(self.base, self.opts.package, True,
                            "\n# Added locks on %s\n" % time.ctime(),
                            ADDING_SPEC, '')
        elif cmd == 'exclude':
            _write_locklist(self.base, self.opts.package, False,
                            "\n# Added exclude on %s\n" % time.ctime(),
                            EXCLUDING_SPEC, '!')
        elif cmd == 'list':
            for pat in _read_locklist():
               logger.info(pat)
        elif cmd == 'clear':
            with open(locklist_fn, 'w') as f:
                # open in write mode truncates file
                pass
        elif cmd == 'delete':
            dirname = os.path.dirname(locklist_fn)
            (out, tmpfilename) = tempfile.mkstemp(dir=dirname, suffix='.tmp')
            locked_specs = _read_locklist()
            count = 0
            with os.fdopen(out, 'w', -1) as out:
                for ent in locked_specs:
                    if _match(ent, self.opts.package):
                        logger.info("%s %s", DELETING_SPEC, ent)
                        count += 1
                        continue
                    out.write(ent)
                    out.write('\n')
            if not count:
                os.unlink(tmpfilename)
            else:
                os.chmod(tmpfilename, 0o644)
                os.rename(tmpfilename, locklist_fn)


def _read_locklist():
    locklist = []
    try:
        with open(locklist_fn) as llfile:
            for line in llfile.readlines():
                if line.startswith('#') or line.strip() == '':
                    continue
                locklist.append(line.strip())
    except IOError as e:
        raise dnf.exceptions.Error(NOT_READABLE % e)
    return locklist

def _write_locklist(base, args, try_installed, comment, info, prefix):
    specs = set()
    for pat in args:
        subj = dnf.subject.Subject(pat)
        pkgs = None
        if try_installed:
            pkgs = subj.get_best_query(dnf.sack.rpmdb_sack(base))
        if not pkgs:
            pkgs = subj.get_best_query(base.sack)
        if not pkgs:
            logger.info("%s %s", NOTFOUND_SPEC, pat)

        for pkg in pkgs:
            specs.add(pkgtup2spec(*pkg.pkgtup))

    with open(locklist_fn, 'a') as f:
        f.write(comment)
        for spec in specs:
            logger.info("%s %s", info, spec)
            f.write("%s%s\n" % (prefix, spec))

def _match(ent, patterns):
    try:
        n = hawkey.split_nevra(ent)
    except hawkey.ValueException:
        return False
    for name in (
        '%s' % n.name,
        '%s.%s' % (n.name, n.arch),
        '%s-%s' % (n.name, n.version),
        '%s-%s-%s' % (n.name, n.version, n.release),
        '%s-%s-%s.%s' % (n.name, n.version, n.release, n.arch),
        '%s:%s-%s-%s.%s' % (n.epoch, n.name, n.version, n.release, n.arch),
        '%s-%s:%s-%s.%s' % (n.name, n.epoch, n.version, n.release, n.arch),
    ):
        for pat in patterns:
            if fnmatch.fnmatch(name, pat):
                return True
    return False

def pkgtup2spec(name, arch, epoch, version, release):
    # we ignore arch
    e = "" if epoch in (None, "") else "%s:" % epoch
    return "%s-%s%s-%s.*" % (name, e, version, release)
