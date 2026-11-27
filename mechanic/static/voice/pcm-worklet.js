class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.packet = new ArrayBuffer(1600);
    this.samples = new DataView(this.packet);
    this.offset = 0;
    this.enabled = true;
    this.port.onmessage = ({ data }) => {
      this.enabled = data.enabled;
      this.offset = 0;
    };
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!this.enabled || !channel) return true;
    for (const sample of channel) {
      const bounded = Math.max(-1, Math.min(1, sample));
      this.samples.setInt16(this.offset, bounded * (bounded < 0 ? 32768 : 32767), true);
      this.offset += 2;
      if (this.offset === this.packet.byteLength) {
        this.port.postMessage(this.packet, [this.packet]);
        this.packet = new ArrayBuffer(1600);
        this.samples = new DataView(this.packet);
        this.offset = 0;
      }
    }
    return true;
  }
}

registerProcessor('voice-kit-pcm', PcmCapture);
