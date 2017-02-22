# (c) 2012-2014, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

# Make coding more python3-ish
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import multiprocessing
import socket
import time

from ansible import constants as C
from ansible.plugins import callback
from ansible.utils.color import colorize, hostcolor


def linesplit(socket):
    buff = socket.recv(4096)
    buffering = True
    while buffering:
        if "\n" in buff:
            (line, buff) = buff.split("\n", 1)
            yield line + "\n"
        else:
            more = socket.recv(4096)
            if not more:
                buffering = False
            else:
                buff += more
    if buff:
        yield buff


class CallbackModule(callback.CallbackBase):

    '''
    This is the Zuul streaming callback. It's based on the default
    callback plugin, but streams results from shell commands.
    '''

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'stdout'
    CALLBACK_NAME = 'zuul_stream'

    def __init__(self):

        self._play = None
        self._task = None
        self._last_task_banner = None
        self._untrusted = C.DISPLAY_ARGS_TO_STDOUT
        self._daemon_running = False
        self._daemon_stamp = 'daemon-stamp-%s'
        self._host_dict = {}
        super(CallbackModule, self).__init__()

    def _should_verbose(self, result, level=0):
        return ((self._display.verbosity > level
                 or '_ansible_verbose_always' in result._result)
                and '_ansible_verbose_override' not in result._result)

    def v2_runner_on_failed(self, result, ignore_errors=False):

        if (self._play.strategy == 'free'
                and self._last_task_banner != result._task._uuid):
            self._print_task_banner(result._task)

        delegated_vars = result._result.get('_ansible_delegated_vars', None)
        if 'exception' in result._result:
            if self._display.verbosity < 3:
                # extract just the actual error message from the exception text
                error = result._result['exception'].strip().split('\n')[-1]
                msg = ("An exception occurred during task execution. To see"
                       " the full traceback, use -vvv."
                       " The error was: %s" % error)
            else:
                msg = ("An exception occurred during task execution. The full"
                       " traceback is:\n" + result._result['exception'])

            self._display.display(msg)

        self._handle_warnings(result._result)

        if result._task.loop and 'results' in result._result:
            self._process_items(result)

        else:
            if delegated_vars:
                self._display.display(
                    "fatal: [%s -> %s]: FAILED! => %s" % (
                        result._host.get_name(),
                        delegated_vars['ansible_host'],
                        self._dump_results(result._result)))
            else:
                self._display.display(
                    "fatal: [%s]: FAILED! => %s" % (
                        result._host.get_name(),
                        self._dump_results(result._result)))

        if ignore_errors:
            self._display.display("...ignoring")

    def v2_runner_on_ok(self, result):

        if (self._play.strategy == 'free'
                and self._last_task_banner != result._task._uuid):
            self._print_task_banner(result._task)

        self._clean_results(result._result, result._task.action)

        delegated_vars = result._result.get('_ansible_delegated_vars', None)
        self._clean_results(result._result, result._task.action)
        if result._task.action in ('include', 'include_role'):
            return
        elif result._result.get('changed', False):
            if delegated_vars:
                msg = "changed: [%s -> %s]" % (
                    result._host.get_name(), delegated_vars['ansible_host'])
            else:
                msg = "changed: [%s]" % result._host.get_name()
        else:
            if delegated_vars:
                msg = "ok: [%s -> %s]" % (
                    result._host.get_name(), delegated_vars['ansible_host'])
            else:
                msg = "ok: [%s]" % result._host.get_name()

        self._handle_warnings(result._result)

        if result._task.loop and 'results' in result._result:
            self._process_items(result)
        else:

            if self._should_verbose(result):
                msg += " => %s" % (self._dump_results(result._result),)
            self._display.display(msg)

    def v2_runner_on_skipped(self, result):
        if C.DISPLAY_SKIPPED_HOSTS:
            if (self._play.strategy == 'free'
                    and self._last_task_banner != result._task._uuid):
                self._print_task_banner(result._task)

            if result._task.loop and 'results' in result._result:
                self._process_items(result)
            else:
                msg = "skipping: [%s]" % result._host.get_name()
                if self._should_verbose(result):
                    msg += " => %s" % self._dump_results(result._result)
                self._display.display(msg)

    def v2_runner_on_unreachable(self, result):
        if (self._play.strategy == 'free'
                and self._last_task_banner != result._task._uuid):
            self._print_task_banner(result._task)

        delegated_vars = result._result.get('_ansible_delegated_vars', None)
        if delegated_vars:
            self._display.display(
                "fatal: [%s -> %s]: UNREACHABLE! => %s" % (
                    result._host.get_name(),
                    delegated_vars['ansible_host'],
                    self._dump_results(result._result)))
        else:
            self._display.display(
                "fatal: [%s]: UNREACHABLE! => %s" % (
                    result._host.get_name(),
                    self._dump_results(result._result)))

    def v2_playbook_on_no_hosts_matched(self):
        self._display.display("skipping: no hosts matched")

    def v2_playbook_on_no_hosts_remaining(self):
        self._display.banner("NO MORE HOSTS LEFT")

    def read_log(self, host, ip):
        self._display.display("[%s] starting to log" % host)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        while True:
            try:
                s.connect((ip, 19885))
            except Exception:
                self._display.display("[%s] Waiting on logger" % host)
                time.sleep(0.1)
                continue
            for line in linesplit(s):
                self._display.display("[%s] %s " % (host, line.strip()))

    def v2_playbook_on_task_start(self, task, is_conditional):
        self._task = task

        if self._play.strategy != 'free':
            self._print_task_banner(task)
        if task.action == 'command':
            play_vars = self._play._variable_manager._hostvars
            for host in self._play.hosts:
                ip = play_vars[host]['ansible_host']
                daemon_stamp = self._daemon_stamp % host
                if not os.path.exists(daemon_stamp):
                    self._host_dict[host] = ip
                    open(daemon_stamp, 'w').write('')
                    p = multiprocessing.Process(
                        target=self.read_log, args=(host, ip))
                    p.daemon = True
                    p.start()

    def _print_task_banner(self, task):
        # args can be specified as no_log in several places: in the task or in
        # the argument spec.  We can check whether the task is no_log but the
        # argument spec can't be because that is only run on the target
        # machine and we haven't run it there yet at this time.
        #
        # The zuul runner passes a flag indicating trusted status of a job. We
        # want to not print any args for jobs that are trusted, because those
        # args might have secrets.
        #
        # Those tasks in the trusted jobs should also be explicitly marked
        # no_log - but this should be some additional belt and suspenders.
        args = ''
        if not task.no_log and self._untrusted:
            args = u', '.join(u'%s=%s' % a for a in task.args.items())
            args = u' %s' % args

        self._display.banner(u"TASK [%s%s]" % (task.get_name().strip(), args))
        if self._display.verbosity >= 2:
            path = task.get_path()
            if path:
                self._display.display(u"task path: %s" % path)

        self._last_task_banner = task._uuid

    def v2_playbook_on_cleanup_task_start(self, task):
        self._display.banner("CLEANUP TASK [%s]" % task.get_name().strip())

    def v2_playbook_on_handler_task_start(self, task):
        self._display.banner("RUNNING HANDLER [%s]" % task.get_name().strip())

    def v2_playbook_on_play_start(self, play):
        name = play.get_name().strip()
        if not name:
            msg = u"PLAY"
        else:
            msg = u"PLAY [%s]" % name

        self._play = play

        self._display.banner(msg)

    def v2_on_file_diff(self, result):
        if result._task.loop and 'results' in result._result:
            for res in result._result['results']:
                if 'diff' in res and res['diff'] and res.get('changed', False):
                    diff = self._get_diff(res['diff'])
                    if diff:
                        self._display.display(diff)
        elif ('diff' in result._result and result._result['diff']
              and result._result.get('changed', False)):
            diff = self._get_diff(result._result['diff'])
            if diff:
                self._display.display(diff)

    def v2_runner_item_on_ok(self, result):
        delegated_vars = result._result.get('_ansible_delegated_vars', None)
        if result._task.action in ('include', 'include_role'):
            return
        elif result._result.get('changed', False):
            msg = 'changed'
        else:
            msg = 'ok'

        if delegated_vars:
            msg += ": [%s -> %s]" % (
                result._host.get_name(), delegated_vars['ansible_host'])
        else:
            msg += ": [%s]" % result._host.get_name()

        msg += " => (item=%s)" % (self._get_item(result._result),)

        if self._should_verbose(result):
            msg += " => %s" % self._dump_results(result._result)
        self._display.display(msg)

    def v2_runner_item_on_failed(self, result):
        delegated_vars = result._result.get('_ansible_delegated_vars', None)
        if 'exception' in result._result:
            if self._display.verbosity < 3:
                # extract just the actual error message from the exception text
                error = result._result['exception'].strip().split('\n')[-1]
                msg = ("An exception occurred during task execution."
                       " To see the full traceback, use -vvv. The error was:"
                       " %s") % error
            else:
                msg = ("An exception occurred during task execution. The full"
                       "traceback is:\n" + result._result['exception'])

            self._display.display(msg)

        msg = "failed: "
        if delegated_vars:
            msg += "[%s -> %s]" % (
                result._host.get_name(), delegated_vars['ansible_host'])
        else:
            msg += "[%s]" % (result._host.get_name())

        self._handle_warnings(result._result)
        self._display.display(
            msg + " (item=%s) => %s" % (
                self._get_item(result._result),
                self._dump_results(result._result)))

    def v2_runner_item_on_skipped(self, result):
        if C.DISPLAY_SKIPPED_HOSTS:
            msg = "skipping: [%s] => (item=%s) " % (
                result._host.get_name(), self._get_item(result._result))
            if self._should_verbose(result):
                msg += " => %s" % self._dump_results(result._result)
            self._display.display(msg)

    def v2_playbook_on_include(self, included_file):
        msg = 'included: %s for %s' % (
            included_file._filename,
            ", ".join([h.name for h in included_file._hosts]))
        self._display.display(msg)

    def v2_playbook_on_stats(self, stats):
        self._display.banner("PLAY RECAP")

        hosts = sorted(stats.processed.keys())
        for h in hosts:
            t = stats.summarize(h)

            # False and None mean to not use color. In this case, colorize and
            # hostcolor will be providing formatting, not color.
            self._display.display(u"%s : %s %s %s %s" % (
                hostcolor(h, t, False),
                colorize(u'ok', t['ok'], None),
                colorize(u'changed', t['changed'], None),
                colorize(u'unreachable', t['unreachable'], None),
                colorize(u'failed', t['failures'], None)),
            )
        for host in self._host_dict.keys():
            daemon_stamp = self._daemon_stamp % host
            if os.path.exists(daemon_stamp):
                os.unlink(daemon_stamp)

    def v2_playbook_on_start(self, playbook):
        if self._display.verbosity > 1:
            from os.path import basename
            self._display.banner(
                "PLAYBOOK: %s" % basename(playbook._file_name))

        if self._display.verbosity > 3:
            if self._options is not None:
                for option in dir(self._options):
                    if option.startswith('_') or option in [
                            'read_file', 'ensure_value', 'read_module']:
                        continue
                    val = getattr(self._options, option)
                    if val:
                        self._display.vvvv('%s: %s' % (option, val))

    def v2_runner_retry(self, result):
        msg = "FAILED - RETRYING: %s (%d retries left)." % (
            result._task,
            result._result['retries'] - result._result['attempts'])
        if self._should_verbose(result, level=2):
            msg += "Result was: %s" % self._dump_results(result._result)
        self._display.display(msg, color=C.COLOR_DEBUG)