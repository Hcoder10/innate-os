# Status snapshot for a single training run.

# Run status constants (match server-side RunStatus enum).
uint8 STATUS_UNKNOWN=0
uint8 STATUS_WAITING_FOR_APPROVAL=1
uint8 STATUS_APPROVED=2
uint8 STATUS_REJECTED=3
uint8 STATUS_BOOTING=4
uint8 STATUS_RUNNING=5
uint8 STATUS_DONE=6
uint8 STATUS_DOWNLOADED=7
uint8 STATUS_CANCELLED=8

int32 run_id
uint8 status
string daemon_state         # remote daemon sub-state (too dynamic to enum)
string error_message

builtin_interfaces/Time started_at   # zero if unset
builtin_interfaces/Time finished_at  # zero if unset
string instance_type
string wandb_url             # Weights & Biases run URL ("" until the daemon reports it)

# Live training progress (parsed from the trainer's @@INNATE_PROGRESS marker).
# Both 0 until the first progress marker arrives; total_step==0 => indeterminate.
int32 current_step
int32 total_step

# Run hyperparameters (training_params) as a JSON string, for the webapp's run
# detail pane. "" until known. JSON because the params are arbitrary key/values.
string training_params_json

# Download progress for this run (e.g. downloading results).
bool has_active_transfer
bool transfer_done           # true once a download finished (persists for node lifetime)
innate_cloud_msgs/TransferProgress active_transfer
