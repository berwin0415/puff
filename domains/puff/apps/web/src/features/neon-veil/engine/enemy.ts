import {
  BoxGeometry,
  Color,
  CylinderGeometry,
  Group,
  Mesh,
  MeshBasicMaterial,
  MeshStandardMaterial,
  type Object3D,
  PointLight,
  SphereGeometry,
  TorusGeometry,
  Vector3,
} from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { clamp, damp } from './utils';

export type EnemyType = 'sentry' | 'hunter' | 'heavy';

export interface EnemyConfig {
  label: string;
  hp: number;
  speed: number;
  damage: number;
  fireInterval: number;
  projectileSpeed: number;
  scale: number;
  score: number;
  accent: number;
}

export const ENEMY_CONFIG: Record<EnemyType, EnemyConfig> = {
  sentry: {
    label: '哨兵',
    hp: 96,
    speed: 2.25,
    damage: 11,
    fireInterval: 1.85,
    projectileSpeed: 21,
    scale: 1,
    score: 100,
    accent: 0xff8a3c,
  },
  hunter: {
    label: '猎手',
    hp: 64,
    speed: 3.65,
    damage: 8,
    fireInterval: 1.3,
    projectileSpeed: 25,
    scale: 0.9,
    score: 145,
    accent: 0xff3f74,
  },
  heavy: {
    label: '重装',
    hp: 265,
    speed: 1.55,
    damage: 18,
    fireInterval: 2.5,
    projectileSpeed: 17,
    scale: 1.32,
    score: 300,
    accent: 0xffb02e,
  },
};

export interface EnemyHit {
  distance: number;
  headshot: boolean;
}

export class Enemy {
  readonly group = new Group();
  readonly type: EnemyType;
  readonly config: EnemyConfig;
  readonly baseScale: number;

  hp: number;
  dead = false;
  dying = false;
  deathTime = 0;
  spawnTime = 0;
  spawnDuration = 0.9;
  spawnInvulnerable = true;
  fireCooldown = 1.4 + Math.random() * 1.2;
  telegraph = 0;

  private readonly materials: MeshStandardMaterial[] = [];
  private readonly glows: MeshBasicMaterial[] = [];
  private readonly bodyVisual = new Group();
  private readonly leftLeg = new Group();
  private readonly rightLeg = new Group();
  private readonly rightArm = new Group();
  private readonly leftArm = new Group();
  private readonly head = new Group();
  private readonly visorMaterial: MeshBasicMaterial;
  private readonly coreLight: PointLight;
  private readonly muzzlePoint = new Vector3();
  private hitFlash = 0;
  private deathRotation = 0;
  private walkPhase = 0;

  constructor(type: EnemyType) {
    this.type = type;
    this.config = ENEMY_CONFIG[type];
    this.baseScale = this.config.scale;
    this.hp = this.config.hp;

    this.group.add(this.bodyVisual);
    this.bodyVisual.scale.setScalar(0.02);

    const armor = new MeshStandardMaterial({
      color: 0x1b222b,
      metalness: 0.82,
      roughness: 0.36,
      envMapIntensity: 1.15,
    });
    const dark = new MeshStandardMaterial({
      color: 0x0a0d12,
      metalness: 0.5,
      roughness: 0.55,
      envMapIntensity: 0.8,
    });
    const accent = new MeshStandardMaterial({
      color: 0x39424d,
      metalness: 0.9,
      roughness: 0.28,
      envMapIntensity: 1.4,
    });
    this.materials.push(armor, dark, accent);
    this.visorMaterial = new MeshBasicMaterial({
      color: new Color(this.config.accent).multiplyScalar(2.8),
      toneMapped: false,
    });
    this.glows.push(this.visorMaterial);

    const torso = new Mesh(new RoundedBoxGeometry(0.72, 0.82, 0.4, 4, 0.08), armor);
    torso.position.y = 1.26;
    torso.castShadow = true;
    this.bodyVisual.add(torso);

    const chest = new Mesh(new RoundedBoxGeometry(0.5, 0.42, 0.44, 3, 0.05), accent);
    chest.position.set(0, 1.32, -0.02);
    chest.castShadow = true;
    this.bodyVisual.add(chest);

    const abdomen = new Mesh(new RoundedBoxGeometry(0.42, 0.2, 0.36, 2, 0.04), dark);
    abdomen.position.y = 0.92;
    this.bodyVisual.add(abdomen);
    for (let i = 0; i < 3; i += 1) {
      const vent = new Mesh(new BoxGeometry(0.3, 0.012, 0.02), this.visorMaterial);
      vent.position.set(0, 1.17 + i * 0.05, -0.225);
      this.bodyVisual.add(vent);
    }
    const backpack = new Mesh(new RoundedBoxGeometry(0.5, 0.46, 0.2, 3, 0.05), accent);
    backpack.position.set(0, 1.3, 0.27);
    this.bodyVisual.add(backpack);
    for (const side of [-1, 1]) {
      const tank = new Mesh(new CylinderGeometry(0.07, 0.07, 0.36, 8), dark);
      tank.position.set(side * 0.16, 1.32, 0.36);
      this.bodyVisual.add(tank);
      const tankGlow = new Mesh(new BoxGeometry(0.016, 0.16, 0.01), this.visorMaterial);
      tankGlow.position.set(side * 0.16, 1.32, 0.405);
      this.bodyVisual.add(tankGlow);
    }

    const waist = new Mesh(new CylinderGeometry(0.22, 0.18, 0.24, 12), dark);
    waist.position.y = 0.79;
    this.bodyVisual.add(waist);

    const hip = new Mesh(new RoundedBoxGeometry(0.48, 0.22, 0.32, 2, 0.05), armor);
    hip.position.y = 0.72;
    this.bodyVisual.add(hip);

    this.head.position.y = 1.82;
    this.bodyVisual.add(this.head);
    const skull = new Mesh(new RoundedBoxGeometry(0.34, 0.3, 0.36, 3, 0.07), dark);
    this.head.add(skull);
    const visor = new Mesh(new BoxGeometry(0.26, 0.055, 0.025), this.visorMaterial);
    visor.position.set(0, 0.01, -0.19);
    this.head.add(visor);
    const antenna = new Mesh(new CylinderGeometry(0.008, 0.012, 0.26, 5), accent);
    antenna.position.set(0.13, 0.22, 0.04);
    antenna.rotation.z = -0.2;
    this.head.add(antenna);
    const crest = new Mesh(new BoxGeometry(0.045, 0.05, 0.24), accent);
    crest.position.set(-0.1, 0.19, 0);
    this.head.add(crest);
    for (const side of [-1, 1]) {
      const ear = new Mesh(new RoundedBoxGeometry(0.06, 0.12, 0.16, 2, 0.02), armor);
      ear.position.set(side * 0.19, -0.01, 0.02);
      this.head.add(ear);
    }

    for (const side of [-1, 1]) {
      const shoulder = new Mesh(new SphereGeometry(0.13, 12, 8), accent);
      shoulder.position.set(side * 0.43, 1.48, 0);
      this.bodyVisual.add(shoulder);
      const pauldron = new Mesh(new RoundedBoxGeometry(0.24, 0.11, 0.34, 2, 0.035), armor);
      pauldron.position.set(side * 0.45, 1.55, 0);
      pauldron.rotation.z = side * -0.18;
      pauldron.castShadow = true;
      this.bodyVisual.add(pauldron);
      const pauldronLight = new Mesh(new BoxGeometry(0.02, 0.012, 0.2), this.visorMaterial);
      pauldronLight.position.set(side * 0.56, 1.58, 0);
      this.bodyVisual.add(pauldronLight);
    }

    this.rightArm.position.set(0.43, 1.46, 0);
    this.bodyVisual.add(this.rightArm);
    const rightUpper = new Mesh(new CylinderGeometry(0.075, 0.09, 0.48, 10), dark);
    rightUpper.position.y = -0.22;
    this.rightArm.add(rightUpper);
    const rightFore = new Mesh(new CylinderGeometry(0.055, 0.07, 0.38, 10), armor);
    rightFore.position.set(0, -0.5, -0.12);
    rightFore.rotation.x = -0.6;
    this.rightArm.add(rightFore);
    const rifle = new Mesh(new RoundedBoxGeometry(0.09, 0.12, 0.52, 2, 0.02), dark);
    rifle.position.set(0.02, -0.62, -0.28);
    rifle.rotation.x = -0.08;
    this.rightArm.add(rifle);
    const rifleGlow = new Mesh(new BoxGeometry(0.012, 0.02, 0.2), this.visorMaterial);
    rifleGlow.position.set(0.02, -0.57, -0.36);
    this.rightArm.add(rifleGlow);

    this.leftArm.position.set(-0.43, 1.46, 0);
    this.bodyVisual.add(this.leftArm);
    const leftUpper = new Mesh(new CylinderGeometry(0.072, 0.088, 0.46, 10), dark);
    leftUpper.position.y = -0.21;
    this.leftArm.add(leftUpper);
    const leftFore = new Mesh(new CylinderGeometry(0.052, 0.068, 0.36, 10), armor);
    leftFore.position.set(0, -0.48, -0.05);
    leftFore.rotation.x = -0.32;
    this.leftArm.add(leftFore);
    const blade = new Mesh(new BoxGeometry(0.035, 0.38, 0.09), accent);
    blade.position.set(0, -0.66, 0.03);
    blade.rotation.x = 0.1;
    this.leftArm.add(blade);

    this.leftLeg.position.set(-0.19, 0.68, 0);
    this.rightLeg.position.set(0.19, 0.68, 0);
    this.bodyVisual.add(this.leftLeg, this.rightLeg);
    for (const leg of [this.leftLeg, this.rightLeg]) {
      const thigh = new Mesh(new CylinderGeometry(0.095, 0.085, 0.42, 10), dark);
      thigh.position.y = -0.2;
      leg.add(thigh);
      const shin = new Mesh(new RoundedBoxGeometry(0.14, 0.4, 0.16, 2, 0.03), armor);
      shin.position.set(0, -0.52, 0);
      leg.add(shin);
      const knee = new Mesh(new SphereGeometry(0.075, 10, 7), accent);
      knee.position.set(0, -0.33, -0.075);
      leg.add(knee);
      const shinLight = new Mesh(new BoxGeometry(0.02, 0.22, 0.012), this.visorMaterial);
      shinLight.position.set(0, -0.52, -0.086);
      leg.add(shinLight);
      const foot = new Mesh(new RoundedBoxGeometry(0.17, 0.08, 0.27, 2, 0.02), accent);
      foot.position.set(0, -0.73, -0.05);
      leg.add(foot);
    }

    const core = new Mesh(new TorusGeometry(0.11, 0.018, 6, 20), this.visorMaterial);
    core.rotation.x = Math.PI / 2;
    core.position.set(0, 1.5, -0.25);
    this.bodyVisual.add(core);
    const coreInner = new Mesh(new SphereGeometry(0.055, 10, 8), this.visorMaterial);
    coreInner.position.set(0, 1.5, -0.25);
    this.bodyVisual.add(coreInner);

    this.coreLight = new PointLight(this.config.accent, 6, 4, 2);
    this.coreLight.position.set(0, 1.5, -0.3);
    this.bodyVisual.add(this.coreLight);

    this.bodyVisual.scale.setScalar(this.baseScale);
    this.group.userData.enemy = this;
  }

  get position() {
    return this.group.position;
  }

  get active() {
    return !this.dead && !this.dying && this.spawnTime >= this.spawnDuration;
  }

  get healthRatio() {
    return clamp(this.hp / this.config.hp, 0, 1);
  }

  getBodyCenter(target: Vector3) {
    return target.copy(this.group.position).setY(this.group.position.y + 1.2 * this.baseScale);
  }

  getHeadCenter(target: Vector3) {
    return target.copy(this.group.position).setY(this.group.position.y + 1.84 * this.baseScale);
  }

  get bodyRadius() {
    return 0.46 * this.baseScale;
  }

  get headRadius() {
    return 0.22 * this.baseScale;
  }

  getMuzzle(target: Vector3) {
    this.muzzlePoint.set(0.45, 1.05, -0.6);
    return target.copy(this.muzzlePoint).applyMatrix4(this.group.matrixWorld);
  }

  takeDamage(amount: number, headshot: boolean) {
    if (this.dead || this.dying || this.spawnInvulnerable) {
      return false;
    }
    this.hp -= amount;
    this.hitFlash = 0.1;
    if (headshot) {
      this.hitFlash = 0.16;
    }
    if (this.hp <= 0) {
      this.hp = 0;
      this.startDeath();
      return true;
    }
    return false;
  }

  private startDeath() {
    this.dying = true;
    this.deathTime = 0;
    this.deathRotation = 0;
    this.visorMaterial.color.setRGB(0.5, 0.08, 0.02);
  }

  update(dt: number, time: number, moving: number, aiming: boolean) {
    this.spawnTime += dt;
    if (this.dying) {
      this.deathTime += dt;
      this.deathRotation = damp(this.deathRotation, -1.35, 5.5, dt);
      this.group.rotation.x = this.deathRotation;
      this.group.position.y = Math.max(-0.5, this.group.position.y - dt * 0.18);
      if (this.deathTime > 2.4) {
        this.dead = true;
      }
      return;
    }

    const spawnProgress = clamp(this.spawnTime / this.spawnDuration, 0, 1);
    const eased = 1 - (1 - spawnProgress) ** 3;
    const scale = this.baseScale * eased;
    this.bodyVisual.scale.setScalar(scale);
    this.spawnInvulnerable = spawnProgress < 0.72;

    if (this.spawnTime < this.spawnDuration) {
      this.group.rotation.y += dt * 2.2;
    }

    this.walkPhase += dt * (4.5 + moving * 6);
    const swing = Math.sin(this.walkPhase) * 0.55 * Math.min(1, moving);
    this.leftLeg.rotation.x = swing;
    this.rightLeg.rotation.x = -swing;
    this.leftArm.rotation.x = -swing * 0.4;
    this.rightArm.rotation.x = aiming ? -1.3 : -0.25 + swing * 0.18;
    this.head.rotation.x = Math.sin(time * 1.7) * 0.03;
    this.coreLight.intensity = 5 + Math.sin(time * 4.2) * 1.4;

    if (this.hitFlash > 0) {
      this.hitFlash -= dt;
      const flash = Math.max(0, this.hitFlash / 0.16);
      for (const material of this.materials) {
        material.emissive.setRGB(flash * 0.9, flash * 0.95, flash);
        material.emissiveIntensity = flash * 1.2;
      }
    } else {
      for (const material of this.materials) {
        material.emissive.setRGB(0, 0, 0);
        material.emissiveIntensity = 0;
      }
    }

    const lowHealth = this.healthRatio < 0.35;
    if (lowHealth) {
      const flicker = Math.sin(time * 23 + this.walkPhase) > 0.45 ? 1 : 0.15;
      this.visorMaterial.color.set(this.config.accent).multiplyScalar(2.3).multiplyScalar(flicker);
    } else {
      this.visorMaterial.color.set(this.config.accent).multiplyScalar(2.8);
    }
  }

  dispose(scene: Object3D) {
    scene.remove(this.group);
    const geometries = new Set<Mesh>();
    this.group.traverse((child) => {
      if (child instanceof Mesh) {
        geometries.add(child);
      }
    });
    for (const mesh of geometries) {
      mesh.geometry.dispose();
    }
    for (const material of this.materials) {
      material.dispose();
    }
    for (const glow of this.glows) {
      glow.dispose();
    }
  }
}
