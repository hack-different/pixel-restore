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

#include "fetch_policy.h"

#include <string>

#include <android-base/file.h>
#include <android-base/parseint.h>
#include <android-base/strings.h>

int GetFetchPolicy() {
    std::string data;
    if (!android::base::ReadFileToString(FASTBOOTD_FETCH_POLICY_PATH, &data)) {
        return kFetchPolicyStock;
    }
    int value = kFetchPolicyStock;
    if (!android::base::ParseInt(android::base::Trim(data), &value)) {
        return kFetchPolicyStock;
    }
    if (value < kFetchPolicyStock || value > kFetchPolicyUnrestricted) {
        return kFetchPolicyStock;
    }
    return value;
}
