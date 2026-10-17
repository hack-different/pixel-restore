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

#include "partition_hash.h"

#include <fcntl.h>
#include <inttypes.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <memory>
#include <vector>

#include <android-base/stringprintf.h>
#include <android-base/strings.h>
#include <ext4_utils/ext4_utils.h>
#include <openssl/evp.h>

#include "fastboot_device.h"
#include "fetch_policy.h"
#include "utility.h"

using android::base::StringPrintf;

namespace {

// Same as kAllowedPartitions in commands.cpp: what stock fastbootd lets
// `fetch` read without any policy change.
constexpr std::array<const char*, 3> kStockAllowedPartitions{
        "vendor_boot",
        "vendor_boot_a",
        "vendor_boot_b",
};

constexpr size_t kReadChunk = 1024 * 1024;
// Send a progress line this often so a long hash (super, userdata) shows life.
constexpr uint64_t kProgressInterval = 1024ULL * 1024 * 1024;

const EVP_MD* DigestNamed(const std::string& name) {
    if (name == "sha256") return EVP_sha256();
    if (name == "sha1") return EVP_sha1();
    if (name == "sha512") return EVP_sha512();
    return nullptr;
}

bool StockAllowed(const std::string& partition) {
    return std::find(kStockAllowedPartitions.begin(), kStockAllowedPartitions.end(), partition) !=
           kStockAllowedPartitions.end();
}

}  // namespace

std::optional<bool> HandlePartitionHash(FastbootDevice* device, const std::string& command) {
    std::vector<std::string> words;
    for (const auto& word : android::base::Split(android::base::Trim(command), " ")) {
        if (!word.empty()) words.push_back(word);
    }
    if (words.size() < 2 || words[0] != "oem") return std::nullopt;
    const std::string& algorithm = words[1];
    const EVP_MD* md = DigestNamed(algorithm);
    if (md == nullptr) return std::nullopt;

    if (words.size() != 3) {
        return device->WriteFail("Usage: oem " + algorithm + " <partition>");
    }
    const std::string& partition = words[2];

    if (GetDeviceLockStatus()) {
        return device->WriteFail("Hashing is not allowed on locked devices");
    }
    if (GetFetchPolicy() < kFetchPolicyReadAll && !StockAllowed(partition)) {
        return device->WriteFail(
                "Hashing " + partition +
                " is not allowed until reading is enabled from the on-screen fastboot menu");
    }

    PartitionHandle handle;
    if (!OpenPartition(device, partition, &handle, O_RDONLY)) {
        return device->WriteFail(StringPrintf("Cannot open %s", partition.c_str()));
    }
    const uint64_t size = get_block_device_size(handle.fd());

    std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> ctx(EVP_MD_CTX_new(),
                                                               EVP_MD_CTX_free);
    if (!ctx || EVP_DigestInit_ex(ctx.get(), md, nullptr) != 1) {
        return device->WriteFail("Cannot start " + algorithm);
    }

    std::vector<uint8_t> buffer(kReadChunk);
    uint64_t done = 0;
    uint64_t next_progress = kProgressInterval;
    while (done < size) {
        const size_t want = static_cast<size_t>(std::min<uint64_t>(buffer.size(), size - done));
        const ssize_t got = TEMP_FAILURE_RETRY(
                pread64(handle.fd(), buffer.data(), want, static_cast<off64_t>(done)));
        if (got <= 0) {
            return device->WriteFail(StringPrintf("Cannot read %s at 0x%" PRIx64,
                                                  partition.c_str(), done));
        }
        EVP_DigestUpdate(ctx.get(), buffer.data(), static_cast<size_t>(got));
        done += static_cast<uint64_t>(got);
        if (done >= next_progress && done < size) {
            device->WriteInfo(StringPrintf("%s: hashed %" PRIu64 " of %" PRIu64 " MiB",
                                           partition.c_str(), done >> 20, size >> 20));
            next_progress += kProgressInterval;
        }
    }

    unsigned char digest[EVP_MAX_MD_SIZE];
    unsigned int digest_len = 0;
    if (EVP_DigestFinal_ex(ctx.get(), digest, &digest_len) != 1) {
        return device->WriteFail("Cannot finish " + algorithm);
    }
    std::string hex;
    for (unsigned int i = 0; i < digest_len; i++) {
        hex += StringPrintf("%02x", digest[i]);
    }

    const std::string result = StringPrintf("%s %s %s %" PRIu64, algorithm.c_str(), hex.c_str(),
                                            partition.c_str(), size);
    device->WriteInfo(result);
    return device->WriteOkay(result);
}
