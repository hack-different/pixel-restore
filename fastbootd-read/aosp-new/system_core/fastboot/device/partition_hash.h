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

#include <optional>
#include <string>

class FastbootDevice;

// On-device partition hashing: `fastboot oem sha256 <partition>` (also sha1,
// sha512). fastbootd reads the whole partition and returns only its digest, so
// a large partition can be checked without transferring it.
//
// Read only, and gated exactly like `fetch`: refused on a locked device, and
// outside the stock fetch allow-list it needs the owner to have picked
// "Enable reading" (or "Enable all partitions") on the on-screen menu.
//
// The digest is sent as an INFO line and as the OKAY message, both in the form
//   "<algorithm> <lowercase hex digest> <partition> <size in bytes>"
//
// `command` is the full OEM command string, e.g. "oem sha256 efs". Returns
// std::nullopt when it is not a hash command (so the caller passes it on to
// the fastboot HAL), otherwise the result of the status written to the host.
std::optional<bool> HandlePartitionHash(FastbootDevice* device, const std::string& command);
