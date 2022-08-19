#!/usr/bin/env python3
#
# Copyright 2019 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import subprocess

import charmhelpers.core.hookenv as hookenv


def create_crush_rule():
    """Create a new CRUSH rule."""
    rule_name = hookenv.action_get('name')
    failure_domain = hookenv.action_get('failure-domain')
    device_class = hookenv.action_get('device-class')
    cmd = [
        'ceph', 'osd', 'crush', 'rule',
        'create-replicated',
        rule_name,
        'default',
        failure_domain
    ]
    if device_class:
        cmd.append(device_class)
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as e:
        hookenv.action_fail(str(e))


if __name__ == '__main__':
    create_crush_rule()
