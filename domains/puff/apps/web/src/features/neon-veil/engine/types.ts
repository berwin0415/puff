export type GamePhase = 'briefing' | 'playing' | 'paused' | 'dead' | 'victory';

export type QualityLevel = 'cinematic' | 'performance';

export type WaveState = 'incoming' | 'active' | 'cleared';

export type GameEventType =
  | 'hit'
  | 'headshot'
  | 'kill'
  | 'damage'
  | 'wave'
  | 'empty'
  | 'reload'
  | 'grenade'
  | 'low-ammo';

export interface GameSnapshot {
  phase: GamePhase;
  health: number;
  maxHealth: number;
  shield: number;
  maxShield: number;
  ammo: number;
  reserve: number;
  magazineSize: number;
  reloading: boolean;
  reloadProgress: number;
  grenadeCooldown: number;
  grenadeReady: boolean;
  wave: number;
  totalWaves: number;
  waveState: WaveState;
  waveCountdown: number;
  enemiesRemaining: number;
  score: number;
  kills: number;
  headshots: number;
  accuracy: number;
  fps: number;
  heading: number;
  sprinting: boolean;
  aiming: boolean;
  crouching: boolean;
  pointerLocked: boolean;
  quality: QualityLevel;
  objective: string;
  hint: string;
  elapsed: number;
}

export interface GameEvent {
  type: GameEventType;
  value?: number;
  label?: string;
  at: number;
}

export interface GameCallbacks {
  onSnapshot: (snapshot: GameSnapshot) => void;
  onEvent: (event: GameEvent) => void;
}

export const EMPTY_SNAPSHOT: GameSnapshot = {
  phase: 'briefing',
  health: 100,
  maxHealth: 100,
  shield: 75,
  maxShield: 75,
  ammo: 32,
  reserve: 192,
  magazineSize: 32,
  reloading: false,
  reloadProgress: 0,
  grenadeCooldown: 0,
  grenadeReady: true,
  wave: 1,
  totalWaves: 5,
  waveState: 'incoming',
  waveCountdown: 3,
  enemiesRemaining: 0,
  score: 0,
  kills: 0,
  headshots: 0,
  accuracy: 0,
  fps: 60,
  heading: 0,
  sprinting: false,
  aiming: false,
  crouching: false,
  pointerLocked: false,
  quality: 'cinematic',
  objective: '守住第七区反应堆',
  hint: '点击进入行动',
  elapsed: 0,
};
