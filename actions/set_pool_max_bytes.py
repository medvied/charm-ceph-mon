#!/usr/bin/env python3
#
# Copyright 2016 Canonical Ltd
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

from subprocess import CalledProcessError
from charmhelpers.core.hookenv import action_get, log, action_fail
from charmhelpers.contrib.storage.linux.ceph import set_pool_quota

if __name__ == '__main__':
    max_bytes = action_get("max")
    name = action_get("name")
    try:
        set_pool_quota(service='admin', pool_name=name, max_bytes=max_bytes)
    except CalledProcessError as e:
        log(e)
        action_fail("Set pool quota failed with message: {}".format(str(e)))
