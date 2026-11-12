import {
  ACESFilmicToneMapping,
  AdditiveBlending,
  BufferGeometry,
  Color,
  Float32BufferAttribute,
  Group,
  Line,
  LineBasicMaterial,
  Mesh,
  MeshBasicMaterial,
  PCFShadowMap,
  PerspectiveCamera,
  PlaneGeometry,
  PointLight,
  Raycaster,
  Scene,
  SphereGeometry,
  Sprite,
  SpriteMaterial,
  SRGBColorSpace,
  type Texture,
  Vector2,
  Vector3,
  WebGLRenderer,
} from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';
import { SMAAPass } from 'three/addons/postprocessing/SMAAPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { SoundEngine } from './audio';
import { Effects } from './effects';
import { ENEMY_CONFIG, Enemy, type EnemyType } from './enemy';
import { InputController } from './input';
import {
  createContactShadowTexture,
  createEnvironmentTexture,
  createGlowTexture,
  createScorchTexture,
  createSmokeTexture,
} from './textures';
import type {
  GameCallbacks,
  GameEventType,
  GamePhase,
  GameSnapshot,
  QualityLevel,
  WaveState,
} from './types';
import { clamp, damp, randomRange, raySphereDistance } from './utils';
import { Weapon } from './weapon';
import { World } from './world';

const TOTAL_WAVES = 5;
const PLAYER_RADIUS = 0.42;
const WALK_SPEED = 4.7;
const SPRINT_SPEED = 6.5;
const CROUCH_SPEED = 2.2;
const GRAVITY = 15.5;
const JUMP_SPEED = 5.4;

const WAVE_PLANS: Array<{ label: string; enemies: EnemyType[] }> = [
  {
    label: '接触',
    enemies: ['sentry', 'sentry', 'sentry', 'sentry'],
  },
  {
    label: '蜂群',
    enemies: ['sentry', 'hunter', 'sentry', 'hunter', 'sentry', 'sentry'],
  },
  {
    label: '重压',
    enemies: ['hunter', 'hunter', 'sentry', 'sentry', 'heavy'],
  },
  {
    label: '破门',
    enemies: ['hunter', 'sentry', 'sentry', 'heavy', 'hunter', 'sentry'],
  },
  {
    label: '终局',
    enemies: ['heavy', 'hunter', 'hunter', 'sentry', 'sentry', 'heavy', 'hunter', 'sentry'],
  },
];

const SPAWN_POINTS = [
  new Vector3(0, 0, -22.6),
  new Vector3(0, 0, 22.6),
  new Vector3(-22.6, 0, 0),
  new Vector3(22.6, 0, 0),
];

// Bloom 在 HDR 半浮点链里遇到 NaN/Inf 会把整屏染黑；先净化再泛光，
// 这是本作后处理链稳定性的关键。
const SANITIZE_SHADER = {
  uniforms: {
    tDiffuse: { value: null },
  },
  vertexShader: `
    varying vec2 vUv;
    void main() {
      vUv = uv;
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }
  `,
  fragmentShader: `
    uniform sampler2D tDiffuse;
    varying vec2 vUv;
    void main() {
      vec4 color = texture2D(tDiffuse, vUv);
      if (isnan(color.r) || isinf(color.r)) color.r = 0.0;
      if (isnan(color.g) || isinf(color.g)) color.g = 0.0;
      if (isnan(color.b) || isinf(color.b)) color.b = 0.0;
      gl_FragColor = vec4(clamp(color.rgb, 0.0, 24.0), 1.0);
    }
  `,
};

const CINEMATIC_SHADER = {
  uniforms: {
    tDiffuse: { value: null },
    uTime: { value: 0 },
    uDamage: { value: 0 },
    uLowHealth: { value: 0 },
    uResolution: { value: new Vector2(1, 1) },
  },
  vertexShader: `
    varying vec2 vUv;
    void main() {
      vUv = uv;
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }
  `,
  fragmentShader: `
    uniform sampler2D tDiffuse;
    uniform float uTime;
    uniform float uDamage;
    uniform float uLowHealth;
    uniform vec2 uResolution;
    varying vec2 vUv;

    float hash(vec2 p) {
      return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);
    }

    void main() {
      vec2 center = vUv - 0.5;
      float radiusSq = dot(center, center);
      vec2 warpedUv = 0.5 + center * (1.0 + radiusSq * 0.018);
      vec2 aberration = center * (0.0012 + uDamage * 0.0028);
      float red = texture2D(tDiffuse, warpedUv + aberration).r;
      float green = texture2D(tDiffuse, warpedUv).g;
      float blue = texture2D(tDiffuse, warpedUv - aberration).b;
      vec3 color = vec3(red, green, blue);

      float luma = dot(color, vec3(0.299, 0.587, 0.114));
      color = mix(color, color * vec3(0.87, 0.98, 1.08), 0.32);
      color = mix(color, color * vec3(1.08, 1.01, 0.91), smoothstep(0.35, 1.0, luma) * 0.4);
      color = (color - 0.5) * 1.07 + 0.5;
      color += vec3(0.006, 0.009, 0.016);

      float vignette = smoothstep(1.08, 0.24, length(center) * 1.45);
      color *= mix(0.52, 1.0, vignette);
      float edge = smoothstep(0.28, 0.72, length(center));
      color = mix(color, vec3(0.52, 0.015, 0.028), edge * uDamage * 0.72);
      color = mix(color, vec3(0.26, 0.0, 0.01), edge * uLowHealth * 0.22);
      color += (hash(gl_FragCoord.xy + uTime * 61.7) - 0.5) * 0.032;
      color *= 1.0 - 0.012 * (0.5 + 0.5 * sin((vUv.y * uResolution.y) * 2.2));
      gl_FragColor = vec4(color, 1.0);
    }
  `,
};

interface PlayerState {
  position: Vector3;
  velocity: Vector3;
  yaw: number;
  pitch: number;
  eyeHeight: number;
  health: number;
  shield: number;
  grounded: boolean;
  sprinting: boolean;
  crouching: boolean;
  aiming: boolean;
  lastDamageAt: number;
}

interface Projectile {
  group: Group;
  velocity: Vector3;
  life: number;
  damage: number;
  color: number;
}

interface Grenade {
  group: Group;
  velocity: Vector3;
  life: number;
}

export class GameEngine {
  private readonly container: HTMLElement;
  private readonly callbacks: GameCallbacks;
  private readonly renderer: WebGLRenderer;
  private readonly scene = new Scene();
  private readonly weaponScene: Scene;
  private readonly camera = new PerspectiveCamera(74, 1, 0.06, 320);
  private readonly composer: EffectComposer;
  private readonly bloomPass: UnrealBloomPass;
  private readonly gradePass: ShaderPass;
  private readonly world: World;
  private readonly weapon: Weapon;
  private readonly effects: Effects;
  private readonly environment: Texture;
  private readonly effectsGlow: Texture;
  private readonly effectsScorch: Texture;
  private readonly effectsSmoke: Texture;
  private readonly enemyShadowTexture: Texture;
  private readonly audio = new SoundEngine();
  private readonly input: InputController;
  private readonly muzzleLight: PointLight;
  private readonly hitRaycaster = new Raycaster();
  private readonly losRaycaster = new Raycaster();
  private readonly resizeObserver: ResizeObserver;
  private readonly canvas: HTMLCanvasElement;

  private readonly player: PlayerState = {
    position: new Vector3(0, 0, 17.5),
    velocity: new Vector3(),
    yaw: 0,
    pitch: -0.025,
    eyeHeight: 1.68,
    health: 100,
    shield: 75,
    grounded: true,
    sprinting: false,
    crouching: false,
    aiming: false,
    lastDamageAt: -20,
  };

  private readonly enemies: Enemy[] = [];
  private readonly projectiles: Projectile[] = [];
  private readonly grenades: Grenade[] = [];
  private readonly enemyLasers = new Map<Enemy, Line>();

  private phase: GamePhase = 'briefing';
  private quality: QualityLevel;
  private readonly captureMode = new URLSearchParams(window.location.search).has('capture');
  private waveIndex = 0;
  private waveState: WaveState = 'incoming';
  private waveTimer = 4.2;
  private pendingEnemies: EnemyType[] = [];
  private spawnTimer = 0;
  private spawnCursor = 0;
  private score = 0;
  private kills = 0;
  private headshots = 0;
  private shotsFired = 0;
  private shotsHit = 0;
  private grenadeCooldown = 0;

  private elapsed = 0;
  private visualTime = 0;
  private lastFrame = 0;
  private frameHandle = 0;
  private disposed = false;
  private lastSnapshotAt = 0;
  private fps = 60;
  private smoothedFrame = 1 / 60;
  private damageFlash = 0;
  private shake = 0;
  private viewKickPitch = 0;
  private viewKickYaw = 0;
  private bobPhase = 0;
  private landDip = 0;
  private stepTimer = 0;
  private muzzleTimer = 0;
  private hasLocked = false;
  private emptyNoticeCooldown = 0;
  private lowAmmoNotified = false;
  private readonly scratchVector = new Vector3();
  private readonly scratchVector2 = new Vector3();
  private readonly scratchVector3 = new Vector3();

  constructor(
    container: HTMLElement,
    callbacks: GameCallbacks,
    quality: QualityLevel = 'cinematic',
  ) {
    this.container = container;
    this.callbacks = callbacks;
    this.quality = quality;

    this.renderer = new WebGLRenderer({
      antialias: false,
      powerPreference: 'high-performance',
      alpha: false,
      stencil: false,
    });
    this.canvas = this.renderer.domElement;
    this.canvas.style.display = 'block';
    this.canvas.style.width = '100%';
    this.canvas.style.height = '100%';
    this.canvas.style.outline = 'none';
    this.container.appendChild(this.canvas);

    this.renderer.outputColorSpace = SRGBColorSpace;
    this.renderer.toneMapping = ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 0.96;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = PCFShadowMap;
    this.renderer.setPixelRatio(this.getPixelRatio());
    this.renderer.setSize(1, 1, false);

    this.environment = createEnvironmentTexture(this.renderer);
    this.scene.background = new Color(0x04060b);
    this.camera.rotation.order = 'YXZ';
    this.scene.add(this.camera);

    this.world = new World(this.scene, this.renderer, this.environment, quality);
    this.weapon = new Weapon(this.environment);
    this.weaponScene = this.weapon.scene;
    this.effectsGlow = createGlowTexture();
    this.effectsScorch = createScorchTexture();
    this.effectsSmoke = createSmokeTexture();
    this.enemyShadowTexture = createContactShadowTexture();
    this.effects = new Effects(this.scene, this.effectsGlow, this.effectsScorch, this.effectsSmoke);
    this.input = new InputController(this.canvas);
    this.input.onLockChange = (locked) => {
      if (locked) {
        this.hasLocked = true;
      } else if (this.phase === 'playing' && this.hasLocked) {
        this.pause();
      }
      this.emitSnapshot();
    };

    this.muzzleLight = new PointLight(0xffb16a, 0, 9, 2);
    this.scene.add(this.muzzleLight);

    this.composer = new EffectComposer(this.renderer);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    this.composer.addPass(new ShaderPass(SANITIZE_SHADER));
    this.bloomPass = new UnrealBloomPass(new Vector2(1, 1), 0.36, 0.55, 0.94);
    this.composer.addPass(this.bloomPass);
    this.composer.addPass(new OutputPass());
    this.gradePass = new ShaderPass(CINEMATIC_SHADER);
    this.composer.addPass(this.gradePass);
    this.composer.addPass(new SMAAPass());

    this.canvas.addEventListener('click', this.handleCanvasClick);
    document.addEventListener('visibilitychange', this.handleVisibility);
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(this.container);
    this.resize();

    this.player.position.set(0, 0, 17.5);
    this.camera.position.set(0, 1.68, 17.5);
    this.camera.lookAt(0, 2.2, 0);

    if (import.meta.env.DEV) {
      (window as unknown as { __neonEngine?: GameEngine }).__neonEngine = this;
    }
    this.world.setMenuMode(true);
    this.lastFrame = performance.now();
    this.emitSnapshot();
    this.frameHandle = requestAnimationFrame(this.tick);
  }

  private getPixelRatio() {
    const dpr = typeof window === 'undefined' ? 1 : window.devicePixelRatio;
    return Math.min(dpr, this.quality === 'cinematic' ? 1.35 : 1);
  }

  private handleCanvasClick = () => {
    if (this.phase === 'playing' && !this.input.locked && !this.captureMode) {
      this.input.requestLock();
    }
  };

  private handleVisibility = () => {
    if (document.hidden && this.phase === 'playing') {
      this.pause();
    }
  };

  private resize() {
    const width = Math.max(1, this.container.clientWidth);
    const height = Math.max(1, this.container.clientHeight);
    this.renderer.setPixelRatio(this.getPixelRatio());
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.weapon.camera.aspect = width / height;
    this.weapon.camera.updateProjectionMatrix();
    this.composer.setSize(width, height);
    this.composer.setPixelRatio(this.renderer.getPixelRatio());
    const resolution = this.gradePass.uniforms.uResolution;
    if (resolution) {
      resolution.value.set(
        width * this.renderer.getPixelRatio(),
        height * this.renderer.getPixelRatio(),
      );
    }
  }

  setMuted(muted: boolean) {
    this.audio.setMuted(muted);
  }

  setQuality(quality: QualityLevel) {
    if (quality === this.quality) {
      return;
    }
    this.quality = quality;
    this.world.setQuality(quality);
    this.bloomPass.strength = quality === 'cinematic' ? 0.36 : 0.26;
    this.resize();
    this.emitSnapshot();
  }

  start(lockPointer = true) {
    void this.audio.init().catch(() => undefined);
    this.resetGame();
    if (lockPointer && !this.captureMode) {
      this.input.requestLock();
    }
    this.emitSnapshot();
  }

  restart() {
    this.start();
  }

  resume() {
    if (this.phase !== 'paused') {
      return;
    }
    this.phase = 'playing';
    this.audio.setPaused(false);
    this.input.requestLock();
    this.emitSnapshot();
  }

  pause() {
    if (this.phase !== 'playing') {
      return;
    }
    this.phase = 'paused';
    this.audio.setPaused(true);
    this.input.exitLock();
    this.emitSnapshot();
  }

  private resetGame() {
    for (const enemy of this.enemies) {
      this.removeEnemyLaser(enemy);
      enemy.dispose(this.scene);
    }
    this.enemies.length = 0;
    for (const projectile of this.projectiles) {
      this.removeProjectile(projectile);
    }
    this.projectiles.length = 0;
    for (const grenade of this.grenades) {
      this.removeGrenade(grenade);
    }
    this.grenades.length = 0;
    this.effects.reset();
    this.weapon.reset();

    this.player.position.set(0, 0, 17.5);
    this.player.velocity.set(0, 0, 0);
    this.player.yaw = 0;
    this.player.pitch = -0.025;
    this.player.eyeHeight = 1.68;
    this.player.health = 100;
    this.player.shield = 75;
    this.player.grounded = true;
    this.player.sprinting = false;
    this.player.crouching = false;
    this.player.aiming = false;
    this.player.lastDamageAt = -20;

    this.camera.position.set(0, 1.68, 17.5);
    this.camera.rotation.set(-0.025, 0, 0, 'YXZ');
    this.camera.fov = 74;
    this.camera.updateProjectionMatrix();

    this.world.setMenuMode(false);
    this.phase = 'playing';
    this.waveIndex = 0;
    this.waveState = 'incoming';
    this.waveTimer = 4.2;
    this.pendingEnemies = [];
    this.spawnTimer = 0;
    this.spawnCursor = 0;
    this.score = 0;
    this.kills = 0;
    this.headshots = 0;
    this.shotsFired = 0;
    this.shotsHit = 0;
    this.grenadeCooldown = 0;
    this.elapsed = 0;
    this.damageFlash = 0;
    this.shake = 0;
    this.viewKickPitch = 0;
    this.viewKickYaw = 0;
    this.bobPhase = 0;
    this.landDip = 0;
    this.stepTimer = 0;
    this.muzzleTimer = 0;
    this.emptyNoticeCooldown = 0;
    this.lowAmmoNotified = false;
    this.audio.setPaused(false);
    this.emitEvent('wave', 1, WAVE_PLANS[0]?.label ?? '接触');
  }

  private tick = (now: number) => {
    if (this.disposed) {
      return;
    }
    this.frameHandle = requestAnimationFrame(this.tick);
    const rawDelta = (now - this.lastFrame) / 1000;
    this.lastFrame = now;
    const dt = clamp(rawDelta, 0.001, 0.05);
    this.visualTime += dt;
    this.smoothedFrame = damp(this.smoothedFrame, dt, 3, dt);
    this.fps = 1 / Math.max(0.001, this.smoothedFrame);

    if (this.phase === 'briefing') {
      this.updateBriefingCamera();
    } else if (this.phase === 'playing') {
      this.update(dt);
    } else {
      this.updateIdle(dt);
    }

    this.world.update(dt, this.player.position, this.visualTime);
    this.effects.update(dt);
    this.updateMuzzleLight(dt);
    this.updatePostUniforms();
    this.render();
    this.maybeEmitSnapshot(now);
    this.input.endFrame();
  };

  private updateBriefingCamera() {
    const time = this.visualTime;
    const radius = 15.5 + Math.sin(time * 0.09) * 0.9;
    const angle = time * 0.07;
    this.camera.position.set(
      Math.sin(angle) * radius,
      3.8 + Math.sin(time * 0.2) * 0.3,
      Math.cos(angle) * radius,
    );
    this.camera.lookAt(0, 2.35, 0);
    this.camera.fov = 62;
    this.camera.updateProjectionMatrix();
    this.renderer.toneMappingExposure = 1.3;
    this.bloomPass.strength = 0.18;
  }

  private updateIdle(dt: number) {
    this.damageFlash = Math.max(0, this.damageFlash - dt * 1.8);
    this.shake = Math.max(0, this.shake - dt * 2.4);
    if (this.phase === 'paused') {
      return;
    }
    this.camera.position.x += (Math.random() - 0.5) * this.shake * 0.05;
    this.camera.position.y += (Math.random() - 0.5) * this.shake * 0.05;
  }

  private update(dt: number) {
    this.elapsed += dt;
    this.renderer.toneMappingExposure = 0.96;
    this.bloomPass.strength = this.quality === 'cinematic' ? 0.36 : 0.26;
    const delta = this.input.consumeMouseDelta();
    const lookX = this.input.locked ? delta.x : 0;
    const lookY = this.input.locked ? delta.y : 0;
    const sensitivity = this.player.aiming ? 0.00145 : 0.0021;
    if (this.input.locked) {
      this.player.yaw -= lookX * sensitivity;
      this.player.pitch = clamp(this.player.pitch - lookY * sensitivity, -1.35, 1.32);
    }

    const forwardInput = (this.input.isDown('KeyW') ? 1 : 0) - (this.input.isDown('KeyS') ? 1 : 0);
    const strafeInput = (this.input.isDown('KeyD') ? 1 : 0) - (this.input.isDown('KeyA') ? 1 : 0);
    const wish = new Vector3(
      -Math.sin(this.player.yaw) * forwardInput + Math.cos(this.player.yaw) * strafeInput,
      0,
      -Math.cos(this.player.yaw) * forwardInput - Math.sin(this.player.yaw) * strafeInput,
    );
    if (wish.lengthSq() > 0) {
      wish.normalize();
    }

    this.player.crouching = this.input.isDown('ControlLeft') || this.input.isDown('KeyC');
    this.player.aiming = this.input.isMouseDown(2) && this.input.locked;
    this.player.sprinting =
      this.input.isDown('ShiftLeft') &&
      forwardInput > 0 &&
      !this.player.aiming &&
      !this.player.crouching &&
      this.input.locked;

    let speed = WALK_SPEED;
    if (this.player.crouching) {
      speed = CROUCH_SPEED;
    } else if (this.player.sprinting) {
      speed = SPRINT_SPEED;
    } else if (this.player.aiming) {
      speed = 3.2;
    }

    const acceleration = this.player.grounded ? 13 : 2.8;
    this.player.velocity.x = damp(this.player.velocity.x, wish.x * speed, acceleration, dt);
    this.player.velocity.z = damp(this.player.velocity.z, wish.z * speed, acceleration, dt);

    if (this.input.wasPressed('Space') && this.player.grounded) {
      this.player.velocity.y = JUMP_SPEED;
      this.player.grounded = false;
    }
    if (!this.player.grounded) {
      this.player.velocity.y -= GRAVITY * dt;
    }

    const distanceX = this.player.velocity.x * dt;
    const distanceZ = this.player.velocity.z * dt;
    this.moveActor(this.player.position, distanceX, distanceZ, PLAYER_RADIUS);
    this.player.position.y += this.player.velocity.y * dt;
    if (this.player.position.y <= 0) {
      if (!this.player.grounded && this.player.velocity.y < -4) {
        this.landDip = Math.min(0.09, -this.player.velocity.y * 0.012);
        this.shake = Math.max(this.shake, Math.min(0.25, -this.player.velocity.y * 0.018));
        this.audio.footstep(1.5);
      }
      this.player.position.y = 0;
      this.player.velocity.y = 0;
      this.player.grounded = true;
    } else {
      this.player.grounded = false;
    }

    this.player.eyeHeight = damp(
      this.player.eyeHeight,
      this.player.crouching ? 1.12 : 1.68,
      10,
      dt,
    );

    const horizontalSpeed = Math.hypot(this.player.velocity.x, this.player.velocity.z);
    const moveAmount = clamp(horizontalSpeed / WALK_SPEED, 0, 1.4);
    if (this.player.grounded && horizontalSpeed > 0.4) {
      this.bobPhase += dt * (this.player.sprinting ? 13.5 : 9.2) * clamp(moveAmount, 0.5, 1.2);
      this.stepTimer -= dt * (this.player.sprinting ? 1.45 : 1);
      if (this.stepTimer <= 0) {
        this.audio.footstep(clamp(horizontalSpeed / WALK_SPEED, 0.6, 1.25));
        this.stepTimer = 0.4;
      }
    } else {
      this.stepTimer = Math.min(this.stepTimer, 0.1);
    }

    const bobScale = this.player.grounded
      ? clamp(moveAmount, 0, 1) * (1 - (this.player.aiming ? 0.65 : 0))
      : 0;
    const bobY = Math.sin(this.bobPhase * 2) * 0.028 * bobScale;
    const bobX = Math.sin(this.bobPhase) * 0.018 * bobScale;
    this.landDip = damp(this.landDip, 0, 9, dt);
    this.shake = Math.max(0, this.shake - dt * 2.4);
    this.viewKickPitch = damp(this.viewKickPitch, 0, 9, dt);
    this.viewKickYaw = damp(this.viewKickYaw, 0, 9, dt);

    const rightX = Math.cos(this.player.yaw);
    const rightZ = -Math.sin(this.player.yaw);
    this.camera.position.set(
      this.player.position.x + rightX * bobX + (Math.random() - 0.5) * this.shake * 0.08,
      this.player.position.y +
        this.player.eyeHeight +
        bobY -
        this.landDip +
        (Math.random() - 0.5) * this.shake * 0.08,
      this.player.position.z + rightZ * bobX + (Math.random() - 0.5) * this.shake * 0.08,
    );
    const strafeRoll = -strafeInput * 0.018;
    this.camera.rotation.set(
      this.player.pitch + this.viewKickPitch,
      this.player.yaw + this.viewKickYaw,
      strafeRoll,
      'YXZ',
    );
    const targetFov = this.player.aiming ? 53 : this.player.sprinting ? 78 : 74;
    if (Math.abs(this.camera.fov - targetFov) > 0.05) {
      this.camera.fov = damp(this.camera.fov, targetFov, 9, dt);
      this.camera.updateProjectionMatrix();
    }
    this.camera.updateMatrixWorld();

    if (
      this.input.locked &&
      this.player.shield < 75 &&
      this.elapsed - this.player.lastDamageAt > 4
    ) {
      this.player.shield = Math.min(75, this.player.shield + dt * 8.5);
    }
    this.grenadeCooldown = Math.max(0, this.grenadeCooldown - dt);
    this.emptyNoticeCooldown = Math.max(0, this.emptyNoticeCooldown - dt);

    const canAct = this.input.locked;
    const weaponUpdate = this.weapon.update(dt, {
      fire: canAct && this.input.isMouseDown(0),
      aim: this.player.aiming,
      reload: this.input.isDown('KeyR'),
      sprint: this.player.sprinting,
      moving: moveAmount,
      grounded: this.player.grounded,
      lookX,
      lookY,
    });

    for (const event of this.weapon.events) {
      if (event === 'empty') {
        this.audio.empty();
        this.emitEvent('empty');
        this.emptyNoticeCooldown = 1.4;
      } else if (event === 'reload-start') {
        this.audio.reload(0);
        this.emitEvent('reload');
      } else if (event === 'reload-step') {
        this.audio.reload(Math.floor(Math.random() * 2) + 1);
      } else if (event === 'reload-end') {
        this.audio.reload(2);
      }
    }

    if (weaponUpdate.fired) {
      this.fireShot(weaponUpdate.spread);
    }

    if (!this.lowAmmoNotified && this.weapon.ammo <= 6 && !this.weapon.reloading) {
      this.lowAmmoNotified = true;
      this.emitEvent('low-ammo', this.weapon.ammo);
    }
    if (this.weapon.ammo > 8) {
      this.lowAmmoNotified = false;
    }

    if (this.input.wasPressed('KeyQ') || this.input.wasPressed('KeyG')) {
      this.throwGrenade();
    }

    this.damageFlash = Math.max(0, this.damageFlash - dt * 1.7);
    this.updateEnemies(dt);
    this.updateProjectiles(dt);
    this.updateGrenades(dt);
    this.updateWaves(dt);
  }

  private updateMuzzleLight(dt: number) {
    this.muzzleTimer = Math.max(0, this.muzzleTimer - dt);
    this.muzzleLight.intensity = (this.muzzleTimer / 0.055) * 34;
  }

  private updatePostUniforms() {
    this.gradePass.uniforms.uTime.value = this.visualTime;
    this.gradePass.uniforms.uDamage.value = this.damageFlash;
    this.gradePass.uniforms.uLowHealth.value = clamp((35 - this.player.health) / 35, 0, 1);
  }

  private fireShot(spread: number) {
    const forward = this.camera.getWorldDirection(this.scratchVector);
    const right = this.scratchVector2.crossVectors(forward, this.camera.up).normalize();
    const up = this.scratchVector3.crossVectors(right, forward).normalize();
    const direction = forward
      .clone()
      .addScaledVector(right, randomRange(-spread, spread))
      .addScaledVector(up, randomRange(-spread, spread))
      .normalize();

    this.scene.updateMatrixWorld(true);
    this.hitRaycaster.set(this.camera.position, direction);
    this.hitRaycaster.far = 120;
    const worldHits = this.hitRaycaster.intersectObjects(this.world.occluders, false);
    const firstWorldHit = worldHits[0];
    const wallDistance = firstWorldHit?.distance ?? 120;
    const origin = this.camera.position;
    let closestEnemy: Enemy | null = null;
    let closestDistance = wallDistance - 0.15;
    let headshot = false;

    for (const enemy of this.enemies) {
      if (!enemy.active) {
        continue;
      }
      const headCenter = enemy.getHeadCenter(new Vector3());
      const bodyCenter = enemy.getBodyCenter(new Vector3());
      const headDistance = raySphereDistance(origin, direction, headCenter, enemy.headRadius);
      const bodyDistance = raySphereDistance(origin, direction, bodyCenter, enemy.bodyRadius);
      const distance = headDistance ?? bodyDistance;
      if (distance === null || distance >= closestDistance) {
        continue;
      }
      closestEnemy = enemy;
      closestDistance = distance;
      headshot =
        headDistance !== null && (bodyDistance === null || headDistance <= bodyDistance + 0.08);
    }

    const muzzle = origin
      .clone()
      .addScaledVector(right, 0.2)
      .addScaledVector(up, -0.14)
      .addScaledVector(forward, 0.48);
    const endPoint = origin
      .clone()
      .addScaledVector(direction, Math.min(closestDistance, wallDistance));

    this.shotsFired += 1;
    this.muzzleLight.position.copy(origin).addScaledVector(direction, 0.75);
    this.muzzleTimer = 0.055;
    this.audio.shot();
    this.effects.spawnTracer(muzzle, endPoint, 0xffd7a4);
    this.viewKickPitch += randomRange(0.003, 0.007);
    this.viewKickYaw += randomRange(-0.0025, 0.0025);
    this.shake = Math.max(this.shake, 0.08);

    if (closestEnemy) {
      this.shotsHit += 1;
      const hitPoint = origin.clone().addScaledVector(direction, closestDistance);
      const damage = headshot ? 66 : 25;
      const killed = closestEnemy.takeDamage(damage, headshot);
      this.effects.spawnImpact(
        hitPoint,
        direction.clone().negate(),
        closestEnemy.config.accent,
        headshot ? 30 : 18,
      );
      this.audio.hit(headshot);
      this.emitEvent(headshot ? 'headshot' : 'hit', damage);
      if (killed) {
        this.onEnemyKilled(closestEnemy, headshot);
      }
    } else if (firstWorldHit) {
      const normal = firstWorldHit.face
        ? firstWorldHit.face.normal.clone().transformDirection(firstWorldHit.object.matrixWorld)
        : direction.clone().negate();
      this.effects.spawnImpact(firstWorldHit.point, normal);
      this.audio.impact();
    }
  }

  private onEnemyKilled(enemy: Enemy, headshot: boolean) {
    this.kills += 1;
    if (headshot) {
      this.headshots += 1;
    }
    const bonus = headshot ? 1.5 : 1;
    const points = Math.round(enemy.config.score * bonus);
    this.score += points;
    this.effects.spawnEnemyDeath(
      enemy.position.clone().setY(enemy.position.y + 1),
      enemy.config.accent,
    );
    this.audio.kill();
    this.emitEvent('kill', points, `${enemy.config.label}${headshot ? ' · 爆头' : ''}`);
  }

  private updateEnemies(dt: number) {
    const playerChest = new Vector3(
      this.player.position.x,
      this.player.position.y + 1.15,
      this.player.position.z,
    );
    for (let i = this.enemies.length - 1; i >= 0; i -= 1) {
      const enemy = this.enemies[i];
      if (!enemy) {
        continue;
      }
      if (enemy.dead) {
        this.removeEnemyLaser(enemy);
        enemy.dispose(this.scene);
        this.enemies.splice(i, 1);
        continue;
      }

      const toPlayer = this.scratchVector.copy(this.player.position).sub(enemy.position).setY(0);
      const distance = toPlayer.length();
      let moveAmount = 0;

      if (enemy.active && !enemy.dying) {
        const direction = toPlayer.clone().normalize();
        const desiredRange = enemy.type === 'hunter' ? 7.5 : enemy.type === 'heavy' ? 13 : 11.5;
        const desiredDirection = new Vector3();
        if (distance > desiredRange + 2) {
          desiredDirection.copy(direction);
        } else if (distance < desiredRange - 1.5) {
          desiredDirection.copy(direction).multiplyScalar(-1);
        } else {
          desiredDirection
            .set(-direction.z, 0, direction.x)
            .multiplyScalar(enemy.type === 'hunter' ? 1 : -1);
        }

        for (const other of this.enemies) {
          if (other === enemy || other.dead || other.dying) {
            continue;
          }
          const separation = new Vector3().subVectors(enemy.position, other.position).setY(0);
          const separationDistance = separation.length();
          if (separationDistance > 0.001 && separationDistance < 1.8) {
            desiredDirection.addScaledVector(
              separation.normalize(),
              (1.8 - separationDistance) * 0.8,
            );
          }
        }

        if (desiredDirection.lengthSq() > 0.001) {
          desiredDirection.normalize();
          const speed =
            enemy.config.speed * (enemy.type === 'hunter' ? 1 : 0.86) * (distance < 2 ? -0.4 : 1);
          const dx = desiredDirection.x * speed * dt;
          const dz = desiredDirection.z * speed * dt;
          const beforeX = enemy.position.x;
          const beforeZ = enemy.position.z;
          this.moveActor(enemy.position, dx, dz, enemy.bodyRadius * 0.8);
          moveAmount = clamp(
            Math.hypot(enemy.position.x - beforeX, enemy.position.z - beforeZ) / (speed * dt || 1),
            0,
            1,
          );
        }

        const targetYaw = Math.atan2(-direction.x, -direction.z);
        let yawDelta = targetYaw - enemy.group.rotation.y;
        yawDelta = ((yawDelta + Math.PI) % (Math.PI * 2)) - Math.PI;
        enemy.group.rotation.y += yawDelta * Math.min(1, dt * 7);

        enemy.fireCooldown -= dt;
        if (enemy.telegraph > 0) {
          enemy.telegraph -= dt;
          if (enemy.telegraph <= 0) {
            this.spawnProjectile(enemy, playerChest);
            enemy.fireCooldown = enemy.config.fireInterval + randomRange(0.2, 0.8);
          }
        } else if (enemy.fireCooldown <= 0 && distance < 32) {
          const from = enemy.getBodyCenter(new Vector3());
          const to = playerChest;
          const line = to.clone().sub(from);
          const lineLength = line.length();
          this.losRaycaster.set(from, line.normalize());
          this.losRaycaster.far = lineLength - 0.4;
          const blocked =
            this.losRaycaster.intersectObjects(this.world.occluders, false).length > 0;
          if (!blocked) {
            enemy.telegraph = enemy.type === 'heavy' ? 0.5 : 0.3;
          } else {
            enemy.fireCooldown = 0.35;
          }
        }
      }

      enemy.update(dt, this.visualTime, moveAmount, distance < 28);
      this.updateEnemyLaser(enemy, playerChest);
    }
  }

  private updateEnemyLaser(enemy: Enemy, target: Vector3) {
    const existing = this.enemyLasers.get(enemy);
    if (enemy.telegraph <= 0 || enemy.dying || !enemy.active) {
      if (existing) {
        this.scene.remove(existing);
        existing.geometry.dispose();
        (existing.material as LineBasicMaterial).dispose();
        this.enemyLasers.delete(enemy);
      }
      return;
    }
    const origin = enemy.getMuzzle(new Vector3());
    let line = existing;
    if (!line) {
      const geometry = new BufferGeometry();
      geometry.setAttribute(
        'position',
        new Float32BufferAttribute([origin.x, origin.y, origin.z, target.x, target.y, target.z], 3),
      );
      const material = new LineBasicMaterial({
        color: new Color(enemy.config.accent).multiplyScalar(2.4),
        transparent: true,
        opacity: 0.55,
        blending: AdditiveBlending,
        depthWrite: false,
        toneMapped: false,
      });
      line = new Line(geometry, material);
      line.renderOrder = 13;
      this.scene.add(line);
      this.enemyLasers.set(enemy, line);
    }
    const attribute = line.geometry.getAttribute('position');
    attribute.setXYZ(0, origin.x, origin.y, origin.z);
    attribute.setXYZ(1, target.x, target.y, target.z);
    attribute.needsUpdate = true;
    (line.material as LineBasicMaterial).opacity =
      enemy.telegraph < 0.12 ? 0.85 : 0.35 + Math.sin(this.visualTime * 45) * 0.15;
  }

  private spawnProjectile(enemy: Enemy, target: Vector3) {
    enemy.group.updateMatrixWorld(true);
    const origin = enemy.getMuzzle(new Vector3());
    const direction = target.clone().sub(origin).normalize();
    const lead = this.player.velocity.clone().multiplyScalar(0.12);
    direction.add(lead).normalize();
    const group = new Group();
    const color = enemy.config.accent;
    const core = new Mesh(
      new SphereGeometry(0.085, 10, 8),
      new MeshBasicMaterial({
        color: new Color(color).multiplyScalar(2.4),
        toneMapped: false,
      }),
    );
    group.add(core);
    const glow = new Sprite(
      new SpriteMaterial({
        map: this.effectsGlow,
        color,
        transparent: true,
        opacity: 0.85,
        blending: AdditiveBlending,
        depthWrite: false,
        toneMapped: false,
      }),
    );
    glow.scale.setScalar(0.65);
    group.add(glow);
    group.position.copy(origin);
    this.scene.add(group);
    this.projectiles.push({
      group,
      velocity: direction.multiplyScalar(enemy.config.projectileSpeed),
      life: 5,
      damage: enemy.config.damage,
      color,
    });
    this.audio.enemyShot();
  }

  private updateProjectiles(dt: number) {
    const playerChest = new Vector3(
      this.player.position.x,
      this.player.position.y + 1.1,
      this.player.position.z,
    );
    for (let i = this.projectiles.length - 1; i >= 0; i -= 1) {
      const projectile = this.projectiles[i];
      if (!projectile) {
        continue;
      }
      projectile.life -= dt;
      projectile.group.position.addScaledVector(projectile.velocity, dt);
      const position = projectile.group.position;
      let shouldRemove = projectile.life <= 0 || position.y < 0.03;
      if (!shouldRemove) {
        for (const collider of this.world.colliders) {
          if (collider.containsPoint(position)) {
            shouldRemove = true;
            break;
          }
        }
      }
      if (!shouldRemove && position.distanceToSquared(playerChest) < 0.9) {
        this.takeDamage(projectile.damage);
        this.effects.spawnImpact(
          position.clone(),
          projectile.velocity.clone().normalize().negate(),
          projectile.color,
          20,
        );
        shouldRemove = true;
      }
      if (shouldRemove) {
        if (projectile.life > 0 && position.distanceToSquared(playerChest) >= 0.9) {
          this.effects.spawnImpact(
            position.clone(),
            projectile.velocity.clone().normalize().negate(),
            projectile.color,
            12,
          );
        }
        this.removeProjectile(projectile);
        this.projectiles.splice(i, 1);
      }
    }
  }

  private removeEnemyLaser(enemy: Enemy) {
    const line = this.enemyLasers.get(enemy);
    if (!line) {
      return;
    }
    this.scene.remove(line);
    line.geometry.dispose();
    (line.material as LineBasicMaterial).dispose();
    this.enemyLasers.delete(enemy);
  }

  private removeProjectile(projectile: Projectile) {
    this.scene.remove(projectile.group);
    projectile.group.traverse((object) => {
      if (object instanceof Mesh) {
        object.geometry.dispose();
        if (Array.isArray(object.material)) {
          object.material.forEach((material) => {
            material.dispose();
          });
        } else {
          object.material.dispose();
        }
      } else if (object instanceof Sprite) {
        (object.material as SpriteMaterial).dispose();
      }
    });
  }

  private throwGrenade() {
    if (this.grenadeCooldown > 0 || !this.input.locked) {
      return;
    }
    const forward = this.camera.getWorldDirection(new Vector3());
    const group = new Group();
    const body = new Mesh(
      new SphereGeometry(0.09, 12, 8),
      new MeshBasicMaterial({
        color: new Color(0x6ff5ff).multiplyScalar(2.2),
        toneMapped: false,
      }),
    );
    group.add(body);
    const ring = new Sprite(
      new SpriteMaterial({
        map: this.effectsGlow,
        color: 0x6ff5ff,
        transparent: true,
        opacity: 0.8,
        depthWrite: false,
        toneMapped: false,
      }),
    );
    ring.scale.setScalar(0.5);
    group.add(ring);
    group.position.copy(this.camera.position).addScaledVector(forward, 0.5);
    this.scene.add(group);
    this.grenades.push({
      group,
      velocity: forward.multiplyScalar(9).add(new Vector3(0, 3.2, 0)),
      life: 1.55,
    });
    this.grenadeCooldown = 16;
    this.audio.reload(1);
    this.emitEvent('grenade');
  }

  private updateGrenades(dt: number) {
    for (let i = this.grenades.length - 1; i >= 0; i -= 1) {
      const grenade = this.grenades[i];
      if (!grenade) {
        continue;
      }
      grenade.life -= dt;
      grenade.velocity.y -= 18 * dt;
      grenade.group.position.addScaledVector(grenade.velocity, dt);
      if (grenade.group.position.y < 0.12) {
        grenade.group.position.y = 0.12;
        grenade.velocity.y = Math.abs(grenade.velocity.y) * 0.32;
        grenade.velocity.x *= 0.72;
        grenade.velocity.z *= 0.72;
      }
      if (grenade.life <= 0) {
        this.explodeGrenade(grenade.group.position.clone());
        this.removeGrenade(grenade);
        this.grenades.splice(i, 1);
      }
    }
  }

  private explodeGrenade(position: Vector3) {
    this.effects.spawnExplosion(position, 0x6ff5ff);
    this.audio.explosion();
    this.shake = 1.3;
    for (const enemy of this.enemies) {
      if (!enemy.active) {
        continue;
      }
      const distance = enemy.getBodyCenter(new Vector3()).distanceTo(position);
      if (distance > 7) {
        continue;
      }
      const damage = Math.round(165 * (1 - distance / 8.5));
      const killed = enemy.takeDamage(damage, false);
      if (killed) {
        this.onEnemyKilled(enemy, false);
      }
    }
  }

  private removeGrenade(grenade: Grenade) {
    this.scene.remove(grenade.group);
    grenade.group.traverse((object) => {
      if (object instanceof Mesh) {
        object.geometry.dispose();
        if (Array.isArray(object.material)) {
          object.material.forEach((material) => {
            material.dispose();
          });
        } else {
          object.material.dispose();
        }
      } else if (object instanceof Sprite) {
        (object.material as SpriteMaterial).dispose();
      }
    });
  }

  private updateWaves(dt: number) {
    if (this.waveState === 'incoming') {
      this.waveTimer -= dt;
      if (this.waveTimer <= 0) {
        this.waveState = 'active';
        this.pendingEnemies = [...(WAVE_PLANS[this.waveIndex]?.enemies ?? ['sentry'])];
        this.spawnTimer = 0;
      }
      return;
    }

    if (this.waveState === 'active') {
      const activeCount = this.enemies.filter((enemy) => !enemy.dying && !enemy.dead).length;
      const maxActive = Math.min(7, 5 + Math.floor(this.waveIndex / 2));
      this.spawnTimer -= dt;
      if (this.pendingEnemies.length > 0 && activeCount < maxActive && this.spawnTimer <= 0) {
        const type = this.pendingEnemies.shift();
        if (type) {
          this.spawnEnemy(type);
        }
        this.spawnTimer = 0.55;
      }

      if (this.pendingEnemies.length === 0 && activeCount === 0) {
        this.waveState = 'cleared';
        this.waveTimer = this.waveIndex >= TOTAL_WAVES - 1 ? 2.8 : 4.5;
        this.score += 250 * (this.waveIndex + 1);
        this.player.health = Math.min(100, this.player.health + 22);
        this.player.shield = 75;
        this.weapon.reserve = Math.min(384, this.weapon.reserve + 48);
        this.audio.wave();
        this.emitEvent('wave', this.waveIndex + 1, '区域暂时安全');
      }
      return;
    }

    this.waveTimer -= dt;
    if (this.waveTimer > 0) {
      return;
    }
    if (this.waveIndex >= TOTAL_WAVES - 1) {
      this.victory();
      return;
    }
    this.waveIndex += 1;
    this.waveState = 'incoming';
    this.waveTimer = 3.5;
    this.emitEvent('wave', this.waveIndex + 1, WAVE_PLANS[this.waveIndex]?.label ?? '未知');
  }

  private spawnEnemy(type: EnemyType) {
    const enemy = new Enemy(type);
    const spawn = SPAWN_POINTS[this.spawnCursor % SPAWN_POINTS.length] ?? SPAWN_POINTS[0];
    this.spawnCursor += 1;
    enemy.position.set(
      (spawn?.x ?? 0) + randomRange(-0.75, 0.75),
      0,
      (spawn?.z ?? 0) + randomRange(-0.75, 0.75),
    );
    enemy.group.rotation.y = Math.atan2(
      this.player.position.x - enemy.position.x,
      this.player.position.z - enemy.position.z,
    );
    const enemyShadow = new Mesh(
      new PlaneGeometry(1.4, 1.4),
      new MeshBasicMaterial({
        map: this.enemyShadowTexture,
        transparent: true,
        opacity: 0.42,
        depthWrite: false,
        toneMapped: false,
      }),
    );
    enemyShadow.rotation.x = -Math.PI / 2;
    enemyShadow.position.y = 0.025;
    enemyShadow.renderOrder = 3;
    enemy.group.add(enemyShadow);
    this.scene.add(enemy.group);
    this.enemies.push(enemy);
    this.effects.spawnSpawnPillar(enemy.position.clone(), ENEMY_CONFIG[type].accent);
  }

  private takeDamage(amount: number) {
    if (this.phase !== 'playing') {
      return;
    }
    this.player.lastDamageAt = this.elapsed;
    this.damageFlash = Math.min(1, this.damageFlash + 0.55);
    this.shake = Math.max(this.shake, 0.7);
    let remaining = amount;
    if (this.player.shield > 0) {
      const absorbed = Math.min(this.player.shield, remaining);
      this.player.shield -= absorbed;
      remaining -= absorbed;
    }
    if (remaining > 0) {
      this.player.health = Math.max(0, this.player.health - remaining);
    }
    this.audio.hurt();
    this.emitEvent('damage', amount);
    if (this.player.health <= 0) {
      this.die();
    }
  }

  private die() {
    if (this.phase !== 'playing') {
      return;
    }
    this.phase = 'dead';
    this.audio.setPaused(true);
    this.input.exitLock();
    this.emitSnapshot();
  }

  private victory() {
    if (this.phase !== 'playing') {
      return;
    }
    this.phase = 'victory';
    this.audio.victory();
    this.input.exitLock();
    this.emitEvent('wave', TOTAL_WAVES, '区域肃清');
    this.emitSnapshot();
  }

  private moveActor(position: Vector3, dx: number, dz: number, radius: number) {
    if (this.canOccupy(position.x + dx, position.z, radius)) {
      position.x += dx;
    }
    if (this.canOccupy(position.x, position.z + dz, radius)) {
      position.z += dz;
    }
  }

  private canOccupy(x: number, z: number, radius: number) {
    const boundary = 24.9 - radius;
    if (x < -boundary || x > boundary || z < -boundary || z > boundary) {
      return false;
    }
    for (const collider of this.world.colliders) {
      if (collider.max.y < 0.35) {
        continue;
      }
      const closestX = clamp(x, collider.min.x, collider.max.x);
      const closestZ = clamp(z, collider.min.z, collider.max.z);
      const dx = x - closestX;
      const dz = z - closestZ;
      if (dx * dx + dz * dz < radius * radius) {
        return false;
      }
    }
    return true;
  }

  private maybeEmitSnapshot(now: number) {
    if (now - this.lastSnapshotAt > 80) {
      this.emitSnapshot();
    }
  }

  private buildSnapshot(): GameSnapshot {
    const activeCount = this.enemies.filter((enemy) => !enemy.dying && !enemy.dead).length;
    const remaining = activeCount + this.pendingEnemies.length;
    let hint = '保持移动，利用掩体';
    if (this.phase === 'playing') {
      if (!this.input.locked && !this.captureMode) {
        hint = '点击画面锁定鼠标';
      } else if (this.weapon.reloading) {
        hint = '换弹中';
      } else if (this.waveState === 'incoming') {
        hint = `下一波接触 ${Math.max(1, Math.ceil(this.waveTimer))} 秒`;
      } else if (this.emptyNoticeCooldown > 0) {
        hint = '弹匣已空 · 按 R 换弹';
      } else if (this.weapon.ammo <= 6) {
        hint = '弹匣告急 · 按 R 换弹';
      }
    }
    return {
      phase: this.phase,
      health: Math.ceil(this.player.health),
      maxHealth: 100,
      shield: Math.ceil(this.player.shield),
      maxShield: 75,
      ammo: this.weapon.ammo,
      reserve: this.weapon.reserve,
      magazineSize: this.weapon.magazineSize,
      reloading: this.weapon.reloading,
      reloadProgress: this.weapon.reloadProgress,
      grenadeCooldown: this.grenadeCooldown,
      grenadeReady: this.grenadeCooldown <= 0,
      wave: this.waveIndex + 1,
      totalWaves: TOTAL_WAVES,
      waveState: this.waveState,
      waveCountdown: Math.max(0, this.waveTimer),
      enemiesRemaining: remaining,
      score: this.score,
      kills: this.kills,
      headshots: this.headshots,
      accuracy: this.shotsFired === 0 ? 0 : Math.round((this.shotsHit / this.shotsFired) * 100),
      fps: Math.round(this.fps),
      heading: ((-this.player.yaw * 180) / Math.PI + 360) % 360,
      sprinting: this.player.sprinting,
      aiming: this.player.aiming,
      crouching: this.player.crouching,
      pointerLocked: this.input.locked,
      quality: this.quality,
      objective: '守住第七区反应堆',
      hint,
      elapsed: this.elapsed,
    };
  }

  private emitSnapshot() {
    this.lastSnapshotAt = performance.now();
    this.callbacks.onSnapshot(this.buildSnapshot());
  }

  private emitEvent(type: GameEventType, value?: number, label?: string) {
    this.callbacks.onEvent({
      type,
      value,
      label,
      at: Date.now(),
    });
  }

  private render() {
    this.composer.render();
    if (this.phase !== 'briefing') {
      const autoClear = this.renderer.autoClear;
      this.renderer.autoClear = false;
      this.renderer.clearDepth();
      this.renderer.render(this.weaponScene, this.weapon.camera);
      this.renderer.autoClear = autoClear;
    }
  }

  dispose() {
    if (this.disposed) {
      return;
    }
    this.disposed = true;
    cancelAnimationFrame(this.frameHandle);
    this.canvas.removeEventListener('click', this.handleCanvasClick);
    document.removeEventListener('visibilitychange', this.handleVisibility);
    this.resizeObserver.disconnect();
    for (const line of this.enemyLasers.values()) {
      this.scene.remove(line);
      line.geometry.dispose();
      (line.material as LineBasicMaterial).dispose();
    }
    this.enemyLasers.clear();
    this.input.dispose();
    this.audio.dispose();
    this.effects.dispose();
    this.effectsGlow.dispose();
    this.effectsScorch.dispose();
    this.effectsSmoke.dispose();
    this.enemyShadowTexture.dispose();
    this.weapon.dispose();
    this.environment.dispose();
    this.world.dispose();
    this.composer.dispose();
    this.renderer.dispose();
    this.canvas.remove();
  }
}
