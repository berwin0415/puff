/**
 * 全程序化音频：不依赖任何外部音频资源。
 * 枪声、雨声、低频氛围与命中反馈都由 WebAudio 实时合成。
 */
export class SoundEngine {
  private context: AudioContext | null = null;
  private master: GainNode | null = null;
  private reverbSend: GainNode | null = null;
  private ambientGain: GainNode | null = null;
  private noiseBuffer: AudioBuffer | null = null;
  private muted = false;

  async init() {
    if (this.context) {
      if (this.context.state === 'suspended') {
        await this.context.resume();
      }
      return;
    }

    const context = new AudioContext();
    this.context = context;
    const master = context.createGain();
    master.gain.value = this.muted ? 0 : 0.52;
    this.noiseBuffer = this.createNoiseBuffer(2);
    const compressor = context.createDynamicsCompressor();
    compressor.threshold.value = -18;
    compressor.knee.value = 20;
    compressor.ratio.value = 5;
    compressor.attack.value = 0.003;
    compressor.release.value = 0.18;
    master.connect(compressor);
    compressor.connect(context.destination);
    this.master = master;

    const reverb = context.createConvolver();
    reverb.buffer = this.createImpulse(2.4, 2.1);
    const reverbReturn = context.createGain();
    reverbReturn.gain.value = 0.24;
    reverb.connect(reverbReturn);
    reverbReturn.connect(master);
    const reverbSend = context.createGain();
    reverbSend.gain.value = 0.3;
    reverbSend.connect(reverb);
    this.reverbSend = reverbSend;

    this.startAmbience();
    if (context.state === 'suspended') {
      await context.resume();
    }
  }

  setMuted(muted: boolean) {
    this.muted = muted;
    if (this.master && this.context) {
      this.master.gain.setTargetAtTime(muted ? 0 : 0.52, this.context.currentTime, 0.08);
    }
  }

  isMuted() {
    return this.muted;
  }

  private createImpulse(seconds: number, decay: number) {
    const context = this.context;
    if (!context) {
      throw new Error('音频上下文尚未初始化');
    }
    const length = Math.floor(context.sampleRate * seconds);
    const buffer = context.createBuffer(2, length, context.sampleRate);
    for (let channel = 0; channel < 2; channel += 1) {
      const data = buffer.getChannelData(channel);
      for (let i = 0; i < length; i += 1) {
        const envelope = (1 - i / length) ** decay;
        data[i] = (Math.random() * 2 - 1) * envelope;
      }
    }
    return buffer;
  }

  private createNoiseBuffer(seconds = 2) {
    const context = this.context;
    if (!context) {
      throw new Error('音频上下文尚未初始化');
    }
    const length = Math.floor(context.sampleRate * seconds);
    const buffer = context.createBuffer(1, length, context.sampleRate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < length; i += 1) {
      data[i] = Math.random() * 2 - 1;
    }
    return buffer;
  }

  private startAmbience() {
    const context = this.context;
    const master = this.master;
    if (!context || !master) {
      return;
    }

    const ambient = context.createGain();
    ambient.gain.value = 0.42;
    ambient.connect(master);
    this.ambientGain = ambient;

    const rain = context.createBufferSource();
    rain.buffer = this.createNoiseBuffer(4);
    rain.loop = true;
    const rainFilter = context.createBiquadFilter();
    rainFilter.type = 'bandpass';
    rainFilter.frequency.value = 1900;
    rainFilter.Q.value = 0.35;
    const rainGain = context.createGain();
    rainGain.gain.value = 0.055;
    rain.connect(rainFilter);
    rainFilter.connect(rainGain);
    rainGain.connect(ambient);
    rain.start();

    const rumble = context.createBufferSource();
    rumble.buffer = this.createNoiseBuffer(5);
    rumble.loop = true;
    const rumbleFilter = context.createBiquadFilter();
    rumbleFilter.type = 'lowpass';
    rumbleFilter.frequency.value = 130;
    const rumbleGain = context.createGain();
    rumbleGain.gain.value = 0.17;
    rumble.connect(rumbleFilter);
    rumbleFilter.connect(rumbleGain);
    rumbleGain.connect(ambient);
    rumble.start();

    const droneFilter = context.createBiquadFilter();
    droneFilter.type = 'lowpass';
    droneFilter.frequency.value = 320;
    droneFilter.Q.value = 2.5;
    const droneGain = context.createGain();
    droneGain.gain.value = 0.02;
    droneFilter.connect(droneGain);
    droneGain.connect(ambient);
    for (const [frequency, detune] of [
      [55, -7],
      [82.4, 5],
      [110, 11],
    ] as const) {
      const oscillator = context.createOscillator();
      oscillator.type = 'sawtooth';
      oscillator.frequency.value = frequency;
      oscillator.detune.value = detune;
      oscillator.connect(droneFilter);
      oscillator.start();
    }

    const lfo = context.createOscillator();
    lfo.frequency.value = 0.09;
    const lfoGain = context.createGain();
    lfoGain.gain.value = 110;
    lfo.connect(lfoGain);
    lfoGain.connect(droneFilter.frequency);
    lfo.start();
  }

  private get send() {
    return this.reverbSend;
  }

  private makeTone(
    frequency: number,
    endFrequency: number,
    duration: number,
    volume: number,
    type: OscillatorType = 'sine',
    delay = 0,
  ) {
    const context = this.context;
    const master = this.master;
    if (!context || !master) {
      return;
    }
    const start = context.currentTime + delay;
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.type = type;
    oscillator.frequency.setValueAtTime(frequency, start);
    oscillator.frequency.exponentialRampToValueAtTime(Math.max(12, endFrequency), start + duration);
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(volume, start + 0.006);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
    oscillator.connect(gain);
    gain.connect(master);
    if (this.send) {
      gain.connect(this.send);
    }
    oscillator.start(start);
    oscillator.stop(start + duration + 0.02);
  }

  private makeNoise(
    duration: number,
    volume: number,
    filterType: BiquadFilterType,
    frequency: number,
    q = 1,
    delay = 0,
    endFrequency?: number,
  ) {
    const context = this.context;
    const master = this.master;
    if (!context || !master) {
      return;
    }
    const start = context.currentTime + delay;
    const source = context.createBufferSource();
    source.buffer = this.noiseBuffer ?? this.createNoiseBuffer(2);
    const filter = context.createBiquadFilter();
    filter.type = filterType;
    filter.frequency.setValueAtTime(frequency, start);
    if (endFrequency) {
      filter.frequency.exponentialRampToValueAtTime(Math.max(40, endFrequency), start + duration);
    }
    filter.Q.value = q;
    const gain = context.createGain();
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(volume, start + 0.005);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
    source.connect(filter);
    filter.connect(gain);
    gain.connect(master);
    if (this.send) {
      gain.connect(this.send);
    }
    source.start(start);
    source.stop(start + duration + 0.02);
  }

  shot() {
    this.makeNoise(0.16, 0.5, 'bandpass', 1800, 0.9, 0, 220);
    this.makeNoise(0.045, 0.34, 'highpass', 3400, 0.6);
    this.makeTone(128, 42, 0.18, 0.38, 'triangle');
    this.makeTone(620, 190, 0.05, 0.12, 'square');
  }

  enemyShot() {
    this.makeTone(960, 180, 0.18, 0.08, 'sawtooth');
    this.makeNoise(0.12, 0.08, 'bandpass', 900, 2);
  }

  impact() {
    this.makeNoise(0.06, 0.16, 'highpass', 2400, 0.7);
    this.makeTone(1600, 900, 0.04, 0.045, 'square');
  }

  hit(headshot: boolean) {
    const base = headshot ? 1480 : 980;
    this.makeTone(base, base * 1.6, 0.07, 0.085, 'square');
    if (headshot) {
      this.makeTone(base * 2, base * 2.4, 0.1, 0.045, 'sine', 0.035);
    }
  }

  kill() {
    this.makeTone(300, 760, 0.16, 0.07, 'triangle');
    this.makeTone(600, 1100, 0.22, 0.04, 'sine', 0.04);
  }

  hurt() {
    this.makeNoise(0.2, 0.2, 'lowpass', 320, 0.9);
    this.makeTone(84, 38, 0.26, 0.22, 'sine');
  }

  reload(step: number) {
    const frequency = step === 0 ? 680 : step === 1 ? 380 : 920;
    this.makeNoise(0.045, 0.15, 'bandpass', frequency, 6);
    this.makeTone(frequency, frequency * 0.7, 0.035, 0.04, 'square');
  }

  empty() {
    this.makeNoise(0.03, 0.12, 'highpass', 2800, 5);
    this.makeTone(220, 120, 0.04, 0.04, 'square');
  }

  explosion() {
    this.makeNoise(1.2, 0.8, 'lowpass', 900, 0.7, 0, 90);
    this.makeTone(92, 26, 0.85, 0.65, 'sine');
    this.makeTone(240, 40, 0.4, 0.18, 'sawtooth');
  }

  wave() {
    this.makeTone(220, 440, 0.38, 0.09, 'sawtooth');
    this.makeTone(330, 660, 0.42, 0.06, 'triangle', 0.08);
    this.makeTone(440, 880, 0.5, 0.04, 'sine', 0.16);
  }

  victory() {
    for (let i = 0; i < 4; i += 1) {
      this.makeTone(220 * 2 ** (i / 4), 330 * 2 ** (i / 4), 0.8, 0.05, 'triangle', i * 0.18);
    }
  }

  footstep(intensity = 1) {
    this.makeNoise(0.055, 0.075 * intensity, 'bandpass', 480, 1.4);
    this.makeTone(72, 48, 0.09, 0.06 * intensity, 'sine');
  }

  ui() {
    this.makeTone(880, 1320, 0.08, 0.035, 'sine');
  }

  setPaused(paused: boolean) {
    if (!this.context || !this.ambientGain) {
      return;
    }
    this.ambientGain.gain.setTargetAtTime(paused ? 0.12 : 0.42, this.context.currentTime, 0.2);
  }

  dispose() {
    if (this.context) {
      void this.context.close();
      this.context = null;
    }
  }
}
