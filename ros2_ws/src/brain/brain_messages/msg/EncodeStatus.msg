# Background episode-encoding progress, published by the dataset_encoder node on
# /datasets/encode_status. Mirrors the training client's ProgressUpdate so both
# apps can show "conversion in progress…" with per-file progress.
string task_directory   # Skill directory currently being encoded ("" when idle)
string stage            # "idle" | "encoding" | "done" | "error" | "gated"
string current_file     # MP4/H5 file currently being processed
int32 current_index     # 1-based index within the current dataset's work list
int32 total             # total work items for the current dataset
float32 progress        # 0.0–1.0 across the current dataset's work list
string message
string error
int32 queue_depth       # number of datasets still waiting to be processed
