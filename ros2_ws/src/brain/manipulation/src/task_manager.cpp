// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
#include "manipulation/task_manager.hpp"

#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <fstream>
#include <filesystem>
#include <iostream>
#include <hdf5.h>

namespace fs = std::filesystem;

namespace {
// Atomically + durably replace `path` with the pretty-printed JSON `j`: write a
// sibling .tmp, fsync it, rename (atomic on any POSIX filesystem), then fsync the
// directory. A crash or power cut mid-write can't truncate dataset_metadata.json
// to invalid JSON, and the rename can't reach disk ahead of the file's data —
// either would break every later reader (encoder, uploader, the apps' episode
// list). A failed write (disk full / I/O error) throws instead of promoting a
// partial .tmp over the good file.
template <typename Json>
void write_json_atomic(const std::string& path, const Json& j) {
    const std::string tmp_path = path + ".tmp";
    const std::string data = j.dump(4);

    int fd = ::open(tmp_path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        throw std::runtime_error("Failed to open " + tmp_path + " for writing");
    }
    size_t written = 0;
    while (written < data.size()) {
        ssize_t n = ::write(fd, data.data() + written, data.size() - written);
        if (n < 0) {
            ::close(fd);
            throw std::runtime_error("Failed to write " + tmp_path + " (disk full or I/O error)");
        }
        written += static_cast<size_t>(n);
    }
    if (::fsync(fd) != 0) {
        ::close(fd);
        throw std::runtime_error("fsync failed for " + tmp_path);
    }
    ::close(fd);

    fs::rename(tmp_path, path);

    // fsync the directory so the rename itself survives a power cut.
    int dir_fd = ::open(fs::path(path).parent_path().c_str(), O_RDONLY | O_DIRECTORY);
    if (dir_fd >= 0) {
        ::fsync(dir_fd);
        ::close(dir_fd);
    }
}

// Cross-process advisory lock around a dataset_metadata.json read-modify-write.
// The background encoder (Python episode_converter) takes the same flock(2) on
// the same `<path>.lock` file, so episode deletes / outcome edits here can't
// interleave with the encoder's metadata write and resurrect or clobber each
// other. RAII: held for the guard's lifetime, released on close.
class MetadataLock {
   public:
    explicit MetadataLock(const std::string& metadata_path) : lock_path_(metadata_path + ".lock") {
        fd_ = ::open(lock_path_.c_str(), O_CREAT | O_RDWR, 0644);
        if (fd_ >= 0) {
            ::flock(fd_, LOCK_EX);  // blocks until the encoder (if mid-write) releases
        } else {
            // Degrade rather than crash, but say so — without the lock the
            // encoder and this writer can race on dataset_metadata.json.
            std::cerr << "MetadataLock: could not open " << lock_path_ << " (" << std::strerror(errno)
                      << "); proceeding without the cross-process lock" << std::endl;
        }
    }
    ~MetadataLock() {
        if (fd_ >= 0) {
            ::close(fd_);  // closing the fd releases the flock
        }
    }
    MetadataLock(const MetadataLock&) = delete;
    MetadataLock& operator=(const MetadataLock&) = delete;

   private:
    std::string lock_path_;
    int fd_ = -1;
};

// Preserve the dataset_encoder's metadata writes (dataset_type=h264 and the
// per-episode video_files it adds after converting) when the recorder rewrites
// dataset_metadata.json from its in-memory snapshot. That snapshot is taken at
// task activation and never sees the encoder's later disk changes, so without
// this an add_episode / end_task save would downgrade dataset_type back to h5
// and strip video_files — which re-queues the skill into a no-op encode that
// leaves every episode stuck "preparing" with no error. Caller holds the lock.
void merge_encoder_fields_from_disk(const std::string& path, nlohmann::json& meta) {
    if (!fs::exists(path)) {
        return;
    }
    nlohmann::json disk;
    try {
        std::ifstream in(path);
        in >> disk;
    } catch (const std::exception&) {
        return;  // corrupt/unreadable on disk — write our copy as-is rather than fail the save
    }
    if (!disk.is_object()) {
        return;
    }

    // Never downgrade the encoder's h264 marker back to h5.
    if (disk.value("dataset_type", std::string{}) == "h264") {
        meta["dataset_type"] = "h264";
    }

    // Carry over video_files for episodes that already have them on disk and
    // don't in our snapshot (the recorder itself never sets video_files).
    if (!disk.contains("episodes") || !disk["episodes"].is_array() || !meta.contains("episodes") ||
        !meta["episodes"].is_array()) {
        return;
    }
    for (auto& ep : meta["episodes"]) {
        if (!ep.contains("episode_id")) {
            continue;
        }
        if (ep.contains("video_files") && ep["video_files"].is_array() && !ep["video_files"].empty()) {
            continue;
        }
        const int id = ep["episode_id"].get<int>();
        for (const auto& dep : disk["episodes"]) {
            if (dep.value("episode_id", -1) == id && dep.contains("video_files") && dep["video_files"].is_array() &&
                !dep["video_files"].empty()) {
                ep["video_files"] = dep["video_files"];
                break;
            }
        }
    }
}
}  // namespace

namespace manipulation {

TaskManager::TaskManager(const std::string& base_data_directory) : base_data_directory_(base_data_directory) {}

void TaskManager::start_new_task_at_directory(const std::string& task_name, const std::string& task_directory,
                                              double data_frequency) {
    current_task_name_ = task_name;
    current_task_dir_ = task_directory;
    std::string data_dir = current_task_dir_ + "/data";
    std::string dataset_metadata_path = data_dir + "/dataset_metadata.json";

    if (fs::exists(dataset_metadata_path)) {
        std::cout << "Dataset for '" << task_name << "' already exists. Resuming." << std::endl;
        resume_task_at_directory(task_name, task_directory);
        return;
    }

    // Create data directory
    fs::create_directories(data_dir);
    cleanup_stale_streaming_files();

    // Initialize metadata
    metadata_ = {{"data_frequency", data_frequency},
                 {"dataset_type", "h5"},
                 {"number_of_episodes", 0},
                 {"episodes", nlohmann::json::array()}};
    save_metadata();
}

void TaskManager::resume_task_at_directory(const std::string& task_name, const std::string& task_directory) {
    current_task_name_ = task_name;
    current_task_dir_ = task_directory;
    std::string metadata_path = current_task_dir_ + "/data/dataset_metadata.json";

    if (!fs::exists(metadata_path)) {
        std::cerr << "No dataset_metadata.json for '" << task_name << "'. Starting fresh." << std::endl;
        metadata_ = {{"data_frequency", 0},
                     {"dataset_type", "h5"},
                     {"number_of_episodes", 0},
                     {"episodes", nlohmann::json::array()}};
        save_metadata();
        cleanup_stale_streaming_files();
        return;
    }
    load_metadata();
    cleanup_stale_streaming_files();
}

std::string TaskManager::get_streaming_episode_path() const {
    if (current_task_dir_.empty()) {
        throw std::runtime_error("TaskManager: no active task; cannot build streaming path");
    }
    return current_task_dir_ + "/data/.episode_recording.h5.tmp";
}

void TaskManager::cleanup_stale_streaming_files() {
    if (current_task_dir_.empty()) {
        return;
    }
    fs::path stale = fs::path(current_task_dir_) / "data" / ".episode_recording.h5.tmp";
    std::error_code ec;
    if (fs::exists(stale, ec)) {
        std::cerr << "Removing stale streaming file: " << stale.string() << std::endl;
        fs::remove(stale, ec);
    }
}

void TaskManager::add_episode(const std::string& temp_file_path, const std::string& start_timestamp,
                              const std::string& end_timestamp, const std::string& source,
                              const std::string& policy) {
    std::string data_dir = current_task_dir_ + "/data";
    fs::create_directories(data_dir);

    if (!fs::exists(temp_file_path)) {
        throw std::runtime_error("TaskManager::add_episode: streaming file does not exist: " + temp_file_path);
    }

    int episode_id = metadata_["number_of_episodes"].get<int>();
    std::string file_name = "episode_" + std::to_string(episode_id) + ".h5";
    std::string file_path = data_dir + "/" + file_name;

    // Refuse to clobber an existing file. std::filesystem::rename silently
    // replaces the destination on POSIX, which would destroy existing episode
    // data if metadata and disk ever drift out of sync (hand-edited metadata,
    // restore-from-git, partial crash, etc.). Better to fail loudly here and
    // let the operator reconcile than to lose an episode silently.
    if (fs::exists(file_path)) {
        throw std::runtime_error("TaskManager::add_episode: destination already exists; refusing to overwrite '" +
                                 file_path + "' (metadata/disk out of sync?)");
    }

    // Atomic-ish rename (same filesystem) of finalized streaming file into slot.
    std::error_code ec;
    fs::rename(temp_file_path, file_path, ec);
    if (ec) {
        throw std::runtime_error("TaskManager::add_episode: failed to rename '" + temp_file_path + "' -> '" +
                                 file_path + "': " + ec.message());
    }

    // Provenance: `source` records how the episode was produced; `policy` (only
    // for rollouts) records which model drove it. `policy` is written only when
    // present so teleop/replay episodes stay clean.
    nlohmann::json episode_info = {{"episode_id", episode_id},
                                   {"file_name", file_name},
                                   {"start_timestamp", start_timestamp},
                                   {"end_timestamp", end_timestamp},
                                   {"source", source.empty() ? "teleop" : source}};
    if (!policy.empty()) {
        episode_info["policy"] = policy;
    }
    metadata_["episodes"].push_back(episode_info);
    metadata_["number_of_episodes"] = episode_id + 1;
    save_metadata();
}

void TaskManager::end_task() {
    save_metadata();
    current_task_name_.clear();
    current_task_dir_.clear();
    metadata_ = nullptr;
}

int TaskManager::get_number_of_episodes() const {
    if (metadata_.is_null() || !metadata_.contains("number_of_episodes")) {
        return 0;
    }
    return metadata_["number_of_episodes"].get<int>();
}

void TaskManager::save_metadata() {
    if (current_task_dir_.empty()) {
        throw std::runtime_error("No active task directory to save metadata.");
    }
    std::string data_dir = current_task_dir_ + "/data";
    fs::create_directories(data_dir);
    std::string metadata_path = data_dir + "/dataset_metadata.json";

    MetadataLock lock(metadata_path);
    // metadata_ is the snapshot from task activation; the background encoder may
    // have written dataset_type=h264 + per-episode video_files to disk since. The
    // lock stops a torn read but not this stale copy from clobbering those — so
    // merge the encoder-owned fields back from a fresh disk read before writing
    // (the converter does the same re-read on its side).
    merge_encoder_fields_from_disk(metadata_path, metadata_);
    write_json_atomic(metadata_path, metadata_);
}

void TaskManager::load_metadata() {
    if (current_task_dir_.empty()) {
        throw std::runtime_error("No active task directory to load metadata from.");
    }
    std::string metadata_path = current_task_dir_ + "/data/dataset_metadata.json";

    std::ifstream file(metadata_path);
    if (!file.is_open()) {
        std::cerr << "Cannot open " << metadata_path << ". Reinitializing metadata." << std::endl;
        metadata_ = {{"data_frequency", 0},
                     {"dataset_type", "h5"},
                     {"number_of_episodes", 0},
                     {"episodes", nlohmann::json::array()}};
        save_metadata();
        return;
    }
    file >> metadata_;
}

std::optional<nlohmann::json> TaskManager::get_enriched_metadata_for_task(const std::string& task_directory,
                                                                          std::string& error_msg) {
    std::string data_dir = task_directory + "/data";
    std::string metadata_file_path = data_dir + "/dataset_metadata.json";
    std::string task_name = fs::path(task_directory).filename().string();

    // Helper: return a zero-episode response for any "no data yet" state.
    auto empty_metadata = [&]() -> nlohmann::json {
        return {{"task_name", task_name},
                {"task_directory", task_directory},
                {"data_frequency", 0},
                {"number_of_episodes", 0},
                {"episodes", nlohmann::json::array()}};
    };

    // No data yet — folder missing, no data/ subfolder, no metadata file, or empty file.
    if (!fs::exists(task_directory) || !fs::is_directory(task_directory) || !fs::exists(data_dir) ||
        !fs::is_directory(data_dir) || !fs::exists(metadata_file_path) || fs::file_size(metadata_file_path) == 0) {
        return empty_metadata();
    }

    // Try to parse — only error if the file is actually corrupted.
    nlohmann::json dataset_metadata;
    try {
        std::ifstream file(metadata_file_path);
        file >> dataset_metadata;
    } catch (const std::exception& e) {
        error_msg = "Corrupted dataset_metadata.json in " + data_dir + ": " + e.what();
        return std::nullopt;
    }

    // Parsed OK but null / not an object — treat as empty, not corrupted.
    if (dataset_metadata.is_null() || !dataset_metadata.is_object()) {
        return empty_metadata();
    }

    nlohmann::json processed_episodes = nlohmann::json::array();
    if (dataset_metadata.contains("episodes") && dataset_metadata["episodes"].is_array()) {
        for (const auto& episode_info : dataset_metadata["episodes"]) {
            int num_timesteps = 0;
            std::string episode_file_name = episode_info.value("file_name", "");
            std::string episode_file_path = data_dir + "/" + episode_file_name;

            if (!episode_file_name.empty() && fs::exists(episode_file_path)) {
                hid_t file_id = H5Fopen(episode_file_path.c_str(), H5F_ACC_RDONLY, H5P_DEFAULT);
                if (file_id >= 0) {
                    if (H5Lexists(file_id, "/action", H5P_DEFAULT) > 0) {
                        hid_t dataset = H5Dopen2(file_id, "/action", H5P_DEFAULT);
                        if (dataset >= 0) {
                            hid_t dataspace = H5Dget_space(dataset);
                            hsize_t dims[2];
                            H5Sget_simple_extent_dims(dataspace, dims, nullptr);
                            num_timesteps = static_cast<int>(dims[0]);
                            H5Sclose(dataspace);
                            H5Dclose(dataset);
                        }
                    }
                    H5Fclose(file_id);
                }
            }

            processed_episodes.push_back(
                {{"episode_id", "episode_" + std::to_string(episode_info.value("episode_id", 0))},
                 {"start_time", episode_info.value("start_timestamp", "N/A")},
                 {"end_time", episode_info.value("end_timestamp", "N/A")},
                 {"num_timesteps", num_timesteps},
                 {"file_name", episode_file_name},
                 // Per-episode H.264 MP4s (one per camera) once converted; absent
                 // for raw episodes. Lets clients show replay vs "prepare video".
                 {"video_files", episode_info.value("video_files", nlohmann::json::array())},
                 // Curation label ("success"/"failure"/""); absent => unlabeled,
                 // which clients treat as success by default.
                 {"outcome", episode_info.value("outcome", "")},
                 // Provenance: how the episode was produced and, for rollouts,
                 // which model drove it. Legacy episodes predate `source`, so
                 // default to "teleop" (the only pre-provenance recording path).
                 {"source", episode_info.value("source", "teleop")},
                 {"policy", episode_info.value("policy", "")},
                 // Free-form failure-mode tags, absent => none.
                 {"tags", episode_info.value("tags", nlohmann::json::array())}});
        }
    }

    nlohmann::json enriched_metadata = {{"task_name", task_name},
                                        {"task_directory", task_directory},
                                        {"data_frequency", dataset_metadata.value("data_frequency", 0)},
                                        {"dataset_type", dataset_metadata.value("dataset_type", "h5")},
                                        {"number_of_episodes", dataset_metadata.value("number_of_episodes", 0)},
                                        {"episodes", processed_episodes}};

    return enriched_metadata;
}

std::tuple<bool, std::string, std::string> TaskManager::get_task_metadata_by_directory(
    const std::string& task_directory) {
    std::string error_msg;
    auto metadata_opt = get_enriched_metadata_for_task(task_directory, error_msg);

    if (metadata_opt) {
        return {true, "Metadata retrieved successfully.", metadata_opt->dump(4)};
    } else {
        if (error_msg.find("not found") != std::string::npos) {
            return {false, "Task at directory '" + task_directory + "' not found.", "{}"};
        }
        return {false, error_msg, "{}"};
    }
}

std::tuple<bool, std::string> TaskManager::set_episode_outcome(const std::string& task_directory, int episode_id,
                                                               const std::string& outcome,
                                                               const std::vector<std::string>& tags) {
    if (outcome != "success" && outcome != "failure" && !outcome.empty()) {
        return {false, "Invalid outcome '" + outcome + "' (expected success|failure|\"\")"};
    }
    std::string metadata_path = task_directory + "/data/dataset_metadata.json";
    if (!fs::exists(metadata_path)) {
        return {false, "No dataset metadata at " + metadata_path};
    }

    // Serialize the read-modify-write against the background encoder (see
    // MetadataLock) so a concurrent encode write-back can't clobber this outcome.
    MetadataLock lock(metadata_path);

    nlohmann::json meta;
    try {
        std::ifstream in(metadata_path);
        in >> meta;
    } catch (const std::exception& e) {
        return {false, std::string("Failed to parse metadata: ") + e.what()};
    }
    if (!meta.is_object() || !meta.contains("episodes") || !meta["episodes"].is_array()) {
        return {false, "Metadata has no episodes array"};
    }

    bool found = false;
    for (auto& ep : meta["episodes"]) {
        if (ep.value("episode_id", -1) == episode_id) {
            if (outcome.empty()) {
                ep.erase("outcome");
            } else {
                ep["outcome"] = outcome;
            }
            // A non-empty `tags` list replaces the stored tags; an empty list
            // leaves them unchanged. There is no tag-clearing UI, so this keeps
            // an outcome-only relabel (Datasets page) from wiping review tags.
            if (!tags.empty()) {
                ep["tags"] = tags;
            }
            found = true;
            break;
        }
    }
    if (!found) {
        return {false, "Episode " + std::to_string(episode_id) + " not found"};
    }

    try {
        write_json_atomic(metadata_path, meta);
    } catch (const std::exception& e) {
        return {false, std::string("Failed to write metadata: ") + e.what()};
    }

    // Keep the in-memory copy fresh if we just edited the active task.
    if (task_directory == current_task_dir_) {
        metadata_ = meta;
    }
    return {true, "Episode " + std::to_string(episode_id) + " outcome set to '" +
                      (outcome.empty() ? "unlabeled" : outcome) + "'"};
}

std::tuple<bool, std::string> TaskManager::delete_episode(const std::string& task_directory, int episode_id) {
    std::string data_dir = task_directory + "/data";
    std::string metadata_path = data_dir + "/dataset_metadata.json";
    if (!fs::exists(metadata_path)) {
        return {false, "No dataset metadata at " + metadata_path};
    }

    // Hold the lock across the metadata rewrite AND the file removal below, so
    // the background encoder can't re-read a half-deleted state (see MetadataLock).
    MetadataLock lock(metadata_path);

    nlohmann::json meta;
    try {
        std::ifstream in(metadata_path);
        in >> meta;
    } catch (const std::exception& e) {
        return {false, std::string("Failed to parse metadata: ") + e.what()};
    }
    if (!meta.is_object() || !meta.contains("episodes") || !meta["episodes"].is_array()) {
        return {false, "Metadata has no episodes array"};
    }

    auto& episodes = meta["episodes"];
    auto before = episodes.size();
    for (auto it = episodes.begin(); it != episodes.end(); ++it) {
        if (it->value("episode_id", -1) == episode_id) {
            episodes.erase(it);
            break;
        }
    }
    if (episodes.size() == before) {
        return {false, "Episode " + std::to_string(episode_id) + " not found"};
    }

    // number_of_episodes doubles as the next-id counter (see add_episode); leave
    // it untouched so future ids never collide with a deleted slot.
    try {
        write_json_atomic(metadata_path, meta);
    } catch (const std::exception& e) {
        return {false, std::string("Failed to write metadata: ") + e.what()};
    }

    // Remove derived files across data/, raw_data/, thumbs/. Match the episode's
    // own h5 exactly and any "episode_<id>_*" siblings (mp4s, thumbs) — the
    // trailing underscore prevents id 1 from matching episode_10_*.
    const std::string exact_h5 = "episode_" + std::to_string(episode_id) + ".h5";
    const std::string sibling_prefix = "episode_" + std::to_string(episode_id) + "_";
    int removed = 0;
    for (const char* sub : {"/data", "/raw_data", "/thumbs"}) {
        fs::path dir = fs::path(task_directory + sub);
        std::error_code ec;
        if (!fs::is_directory(dir, ec)) {
            continue;
        }
        for (const auto& entry : fs::directory_iterator(dir, ec)) {
            const std::string name = entry.path().filename().string();
            if (name == exact_h5 || name.rfind(sibling_prefix, 0) == 0) {
                std::error_code rm_ec;
                if (fs::remove(entry.path(), rm_ec)) {
                    removed++;
                }
            }
        }
    }

    if (task_directory == current_task_dir_) {
        metadata_ = meta;
    }
    return {true, "Deleted episode " + std::to_string(episode_id) + " (" + std::to_string(removed) + " files)"};
}

std::tuple<bool, std::string, int> TaskManager::copy_episode(const std::string& source_task_directory, int episode_id,
                                                             const std::string& dest_task_directory) {
    std::error_code ec;
    if (fs::weakly_canonical(source_task_directory, ec) == fs::weakly_canonical(dest_task_directory, ec)) {
        return {false, "Source and destination datasets are the same", -1};
    }
    const std::string src_data = source_task_directory + "/data";
    const std::string src_meta_path = src_data + "/dataset_metadata.json";
    if (!fs::exists(src_meta_path)) {
        return {false, "No dataset metadata at " + src_meta_path, -1};
    }

    // Snapshot the source entry under the source lock, then release it — the two
    // locks are never held together, so opposite-direction copies can't deadlock.
    nlohmann::json src_entry;
    nlohmann::json src_meta;
    {
        MetadataLock lock(src_meta_path);
        try {
            std::ifstream in(src_meta_path);
            in >> src_meta;
        } catch (const std::exception& e) {
            return {false, std::string("Failed to parse source metadata: ") + e.what(), -1};
        }
        if (!src_meta.is_object() || !src_meta.contains("episodes") || !src_meta["episodes"].is_array()) {
            return {false, "Source metadata has no episodes array", -1};
        }
        for (const auto& ep : src_meta["episodes"]) {
            if (ep.value("episode_id", -1) == episode_id) {
                src_entry = ep;
                break;
            }
        }
    }
    if (src_entry.is_null()) {
        return {false, "Episode " + std::to_string(episode_id) + " not found in source dataset", -1};
    }

    const std::string dest_data = dest_task_directory + "/data";
    const std::string dest_meta_path = dest_data + "/dataset_metadata.json";
    fs::create_directories(dest_data);

    MetadataLock lock(dest_meta_path);
    nlohmann::json dest_meta;
    if (fs::exists(dest_meta_path) && fs::file_size(dest_meta_path) > 0) {
        try {
            std::ifstream in(dest_meta_path);
            in >> dest_meta;
        } catch (const std::exception& e) {
            return {false, std::string("Failed to parse destination metadata: ") + e.what(), -1};
        }
    } else {
        // Fresh destination (skill created but never recorded into): inherit the
        // source's frequency/type so the copied episode stays self-consistent.
        dest_meta = {{"data_frequency", src_meta.value("data_frequency", 0.0)},
                     {"dataset_type", src_meta.value("dataset_type", "h5")},
                     {"number_of_episodes", 0},
                     {"episodes", nlohmann::json::array()}};
    }
    if (!dest_meta.is_object() || !dest_meta.contains("episodes") || !dest_meta["episodes"].is_array()) {
        return {false, "Destination metadata has no episodes array", -1};
    }
    // Mixed-frequency datasets would silently corrupt training, so refuse.
    const double src_freq = src_meta.value("data_frequency", 0.0);
    const double dest_freq = dest_meta.value("data_frequency", 0.0);
    if (src_freq > 0 && dest_freq > 0 && src_freq != dest_freq) {
        return {false,
                "Destination records at " + std::to_string(dest_freq) + " Hz but the episode was recorded at " +
                    std::to_string(src_freq) + " Hz",
                -1};
    }

    const int new_id = dest_meta.value("number_of_episodes", 0);
    const std::string old_stem = "episode_" + std::to_string(episode_id);
    const std::string new_stem = "episode_" + std::to_string(new_id);

    // Copy the h5 and every "episode_<id>_*" sibling (mp4s, profile trace) from
    // data/ and raw_data/, renaming the stem. Track what we copied so a failure
    // can't leave a half-copied episode behind.
    std::vector<fs::path> copied;
    auto rollback = [&copied]() {
        for (const auto& p : copied) {
            std::error_code rm_ec;
            fs::remove(p, rm_ec);
        }
    };
    if (!fs::exists(src_data + "/" + old_stem + ".h5")) {
        return {false, "Source episode file missing: " + old_stem + ".h5", -1};
    }
    for (const char* sub : {"/data", "/raw_data"}) {
        fs::path src_dir = fs::path(source_task_directory + sub);
        if (!fs::is_directory(src_dir, ec)) {
            continue;
        }
        for (const auto& entry : fs::directory_iterator(src_dir, ec)) {
            const std::string name = entry.path().filename().string();
            // "episode_3.h5" or "episode_3_*" — the underscore/dot boundary keeps
            // episode_3 from matching episode_30's files.
            if (name != old_stem + ".h5" && name.rfind(old_stem + "_", 0) != 0) {
                continue;
            }
            fs::path dest_dir = fs::path(dest_task_directory + sub);
            fs::create_directories(dest_dir, ec);
            fs::path dest_path = dest_dir / (new_stem + name.substr(old_stem.size()));
            std::error_code cp_ec;
            fs::copy_file(entry.path(), dest_path, fs::copy_options::none, cp_ec);
            if (cp_ec) {
                rollback();
                return {false, "Failed to copy " + name + ": " + cp_ec.message(), -1};
            }
            copied.push_back(dest_path);
        }
    }

    // Metadata entry: same provenance/labels, new id and file names.
    nlohmann::json new_entry = src_entry;
    new_entry["episode_id"] = new_id;
    new_entry["file_name"] = new_stem + ".h5";
    if (new_entry.contains("video_files") && new_entry["video_files"].is_array()) {
        for (auto& vf : new_entry["video_files"]) {
            std::string name = vf.get<std::string>();
            if (name.rfind(old_stem + "_", 0) == 0) {
                vf = new_stem + name.substr(old_stem.size());
            }
        }
    }
    dest_meta["episodes"].push_back(new_entry);
    dest_meta["number_of_episodes"] = new_id + 1;
    if (dest_freq <= 0 && src_freq > 0) {
        dest_meta["data_frequency"] = src_freq;
    }
    try {
        write_json_atomic(dest_meta_path, dest_meta);
    } catch (const std::exception& e) {
        rollback();
        return {false, std::string("Failed to write destination metadata: ") + e.what(), -1};
    }

    // If the destination is the actively-recording task, refresh the in-memory
    // snapshot so the recorder's next add_episode doesn't reuse the id we took.
    if (dest_task_directory == current_task_dir_) {
        metadata_ = dest_meta;
    }
    return {true,
            "Copied episode " + std::to_string(episode_id) + " to " +
                fs::path(dest_task_directory).filename().string() + " as episode " + std::to_string(new_id) + " (" +
                std::to_string(copied.size()) + " files)",
            new_id};
}

}  // namespace manipulation
