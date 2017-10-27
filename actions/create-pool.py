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

import sys

sys.path.append('hooks')
from subprocess import CalledProcessError
from charmhelpers.core.hookenv import action_get, log, action_fail
from charmhelpers.contrib.storage.linux.ceph import ErasurePool, ReplicatedPool


def create_pool():
    pool_name = action_get("name")
    pool_type = action_get("pool-type")
    try:
        if pool_type == "replicated":
            replicas = action_get("replicas")
            replicated_pool = ReplicatedPool(name=pool_name,
                                             service='admin',
                                             replicas=replicas)
            replicated_pool.create()

        elif pool_type == "erasure":
            crush_profile_name = action_get("erasure-profile-name")
            erasure_pool = ErasurePool(name=pool_name,
                                       erasure_code_profile=crush_profile_name,
                                       service='admin')
            erasure_pool.create()
        else:
            log("Unknown pool type of {}. Only erasure or replicated is "
                "allowed".format(pool_type))
            action_fail("Unknown pool type of {}. Only erasure or replicated "
                        "is allowed".format(pool_type))
    except CalledProcessError as e:
        action_fail("Pool creation failed because of a failed process. "
                    "Ret Code: {} Message: {}".format(e.returncode, e.message))


if __name__ == '__main__':
    create_pool()
