# PCM audio. S16LE mono 48k is the contract; the typed array is the format.
# rate/channels are self-description for bag readers, not degrees of freedom —
# consumers may assert 48000/1 and reject anything else.
std_msgs/Header header      # stamp = first sample; frame_id identifies the source (e.g. "remote")
uint32  seq                 # per-stream, wraps; gaps = drops
uint32  rate                # always 48000
uint8   channels            # always 1
int16[] samples
