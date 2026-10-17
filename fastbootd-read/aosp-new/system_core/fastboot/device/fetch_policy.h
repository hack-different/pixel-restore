/*
 * Copyright (C) 2026 The pixel-restore authors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
#pragma once

// Runtime "fetch" (partition read) policy for fastbootd.
//
// Upstream fastbootd only lets `fastboot fetch` read a fixed three-entry
// allow-list (vendor_boot, vendor_boot_a, vendor_boot_b). This policy lets the
// phone's owner widen that at run time, but only from the on-screen fastboot
// menu in recovery, behind a warning and a confirmation. Nothing here is
// reachable over USB: the host cannot change the policy, it can only read
// partitions the owner has already unlocked on the device.
//
// The policy is carried in a tiny tmpfs state file so the recovery UI process
// (the writer, which draws the menu) and the fastbootd daemon process (the
// reader, which serves USB) can share it without adding any fastboot/oem
// command. Reading is deliberately fail-safe: any error reads back as stock.
//
// Every policy level is READ ONLY. None of them touches flash, erase, or any
// write path; those keep their upstream locked-device gating untouched.

enum FetchPolicy {
    // Upstream behaviour: fetch is limited to the built-in allow-list.
    kFetchPolicyStock = 0,
    // "Enable reading": fetch is allowed for any partition fastbootd can open
    // by name (modem, efs, efs_backup, persist, devinfo, boot chain, ...).
    // The per-transfer size cap is still enforced.
    kFetchPolicyReadAll = 1,
    // "Enable all partitions": as above, and the per-transfer size cap is also
    // lifted, so arbitrarily large partitions (super, userdata, ...) can be
    // read in one fetch. Still read only.
    kFetchPolicyUnrestricted = 2,
};

// Absolute path of the state file, in the recovery ramdisk tmpfs. Both the
// recovery UI and the fastbootd daemon run from the same ramdisk.
#define FASTBOOTD_FETCH_POLICY_PATH "/dev/fastbootd_fetch_policy"

// Read the current policy. Returns kFetchPolicyStock if the state file is
// absent, unreadable, or malformed, so the safe default always wins.
int GetFetchPolicy();
