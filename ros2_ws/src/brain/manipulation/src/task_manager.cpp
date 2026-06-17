#include "manipulation/task_manager.hpp"

#include <algorithm>
#include <fstream>
#include <filesystem>
#include <iostream>
#include <hdf5.h>

namespace fs = std::filesystem;

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
                              const std::string& end_timestamp) {
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

    nlohmann::json episode_info = {{"episode_id", episode_id},
                                   {"file_name", file_name},
                                   {"start_timestamp", start_timestamp},
                                   {"end_timestamp", end_timestamp}};
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

    std::ofstream file(metadata_path);
    if (!file.is_open()) {
        throw std::runtime_error("Failed to open metadata file for writing: " + metadata_path);
    }
    file << metadata_.dump(4);
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
                 {"outcome", episode_info.value("outcome", "")}});
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
                                                               const std::string& outcome) {
    if (outcome != "success" && outcome != "failure" && !outcome.empty()) {
        return {false, "Invalid outcome '" + outcome + "' (expected success|failure|\"\")"};
    }
    std::string metadata_path = task_directory + "/data/dataset_metadata.json";
    if (!fs::exists(metadata_path)) {
        return {false, "No dataset metadata at " + metadata_path};
    }

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
            found = true;
            break;
        }
    }
    if (!found) {
        return {false, "Episode " + std::to_string(episode_id) + " not found"};
    }

    try {
        std::ofstream out(metadata_path);
        out << meta.dump(4);
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
        std::ofstream out(metadata_path);
        out << meta.dump(4);
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

}  // namespace manipulation
