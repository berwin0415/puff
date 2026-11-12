import {
  AdditiveBlending,
  AmbientLight,
  BoxGeometry,
  CanvasTexture,
  Color,
  CylinderGeometry,
  DirectionalLight,
  DoubleSide,
  Group,
  Mesh,
  MeshBasicMaterial,
  type MeshPhysicalMaterial,
  MeshStandardMaterial,
  NormalBlending,
  PerspectiveCamera,
  PlaneGeometry,
  PointLight,
  Scene,
  Sprite,
  SpriteMaterial,
  SRGBColorSpace,
  type Texture,
  TorusGeometry,
  Vector3,
} from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { clamp, damp, lerp, randomRange } from './utils';

export type WeaponEvent = 'empty' | 'reload-start' | 'reload-step' | 'reload-end';

export interface WeaponInput {
  fire: boolean;
  aim: boolean;
  reload: boolean;
  sprint: boolean;
  moving: number;
  grounded: boolean;
  lookX: number;
  lookY: number;
}

export interface WeaponUpdate {
  fired: boolean;
  spread: number;
}

interface Shell {
  mesh: Mesh;
  velocity: Vector3;
  life: number;
}

const MAGAZINE_SIZE = 32;
const RESERVE_SIZE = 192;
const FIRE_INTERVAL = 0.078;
const RELOAD_DURATION = 1.72;

export class Weapon {
  readonly scene = new Scene();
  readonly camera = new PerspectiveCamera(58, 1, 0.01, 6);

  ammo = MAGAZINE_SIZE;
  reserve = RESERVE_SIZE;
  reloading = false;
  reloadProgress = 0;
  readonly magazineSize = MAGAZINE_SIZE;

  events: WeaponEvent[] = [];

  private readonly root = new Group();
  private readonly muzzle = new Vector3(0, 0.02, -0.78);
  private readonly basePosition = new Vector3(0.24, -0.19, -0.8);
  private readonly adsPosition = new Vector3(0, -0.15, -0.62);
  private readonly baseRotation = new Vector3(0.015, -0.12, 0.015);
  private readonly flash: Sprite;
  private readonly flashLight: PointLight;
  private readonly shells: Shell[] = [];
  private readonly magazineMesh: Mesh;
  private readonly muzzleSmoke: Sprite[] = [];
  private readonly glowTexture: Texture;
  private readonly materials: Array<MeshStandardMaterial | MeshPhysicalMaterial> = [];

  private cooldown = 0;
  private flashTimer = 0;
  private recoil = 0;
  private ads = 0;
  private bobTime = 0;
  private swayX = 0;
  private swayY = 0;
  private previousFire = false;
  private previousReload = false;
  private reloadStep = 0;
  private sprintAmount = 0;
  private elapsed = 0;

  constructor(environment: Texture) {
    this.scene.environment = environment;
    this.scene.environmentIntensity = 0.85;
    this.glowTexture = this.makeGlowTexture();

    const gunmetal = new MeshStandardMaterial({
      color: 0x343b43,
      metalness: 0.52,
      roughness: 0.4,
      envMapIntensity: 1.05,
    });
    const darkMetal = new MeshStandardMaterial({
      color: 0x0d1014,
      metalness: 0.55,
      roughness: 0.5,
      envMapIntensity: 0.8,
    });
    const polymer = new MeshStandardMaterial({
      color: 0x252b31,
      metalness: 0.14,
      roughness: 0.68,
      envMapIntensity: 0.75,
    });
    const edge = new MeshStandardMaterial({
      color: 0x707b86,
      metalness: 0.72,
      roughness: 0.3,
      envMapIntensity: 1.2,
    });
    const rubber = new MeshStandardMaterial({
      color: 0x080a0c,
      metalness: 0.05,
      roughness: 0.92,
    });
    const brass = new MeshStandardMaterial({
      color: 0xc99a4a,
      metalness: 1,
      roughness: 0.22,
    });
    const cyanGlow = new MeshBasicMaterial({
      color: new Color(0x69f7ff).multiplyScalar(2.7),
      toneMapped: false,
    });
    const magentaGlow = new MeshBasicMaterial({
      color: new Color(0xff4b9c).multiplyScalar(2.2),
      toneMapped: false,
    });
    const amberGlow = new MeshBasicMaterial({
      color: new Color(0xff9a3d).multiplyScalar(2.1),
      toneMapped: false,
    });
    this.materials.push(gunmetal, darkMetal, polymer, rubber, brass, edge);

    this.root.position.copy(this.basePosition);
    this.root.rotation.set(this.baseRotation.x, this.baseRotation.y, this.baseRotation.z);
    this.root.scale.setScalar(0.9);
    this.scene.add(this.root);

    const body = new Group();
    this.root.add(body);

    const receiver = new Mesh(new RoundedBoxGeometry(0.115, 0.115, 0.34, 4, 0.018), gunmetal);
    receiver.position.set(0, 0.012, -0.055);
    body.add(receiver);
    const receiverTop = new Mesh(new RoundedBoxGeometry(0.098, 0.032, 0.3, 3, 0.01), darkMetal);
    receiverTop.position.set(0, 0.078, -0.06);
    body.add(receiverTop);
    const topPanel = new Mesh(new RoundedBoxGeometry(0.076, 0.008, 0.22, 2, 0.003), edge);
    const topGlow = new Mesh(new BoxGeometry(0.02, 0.002, 0.11), cyanGlow);
    topGlow.position.set(0, 0.101, -0.1);
    body.add(topGlow);

    topPanel.position.set(0, 0.095, -0.1);
    body.add(topPanel);
    const sidePanel = new Mesh(new BoxGeometry(0.004, 0.052, 0.16), edge);
    sidePanel.position.set(0.0585, 0.018, -0.12);
    body.add(sidePanel);

    const handguard = new Mesh(new RoundedBoxGeometry(0.092, 0.09, 0.36, 4, 0.016), polymer);
    handguard.position.set(0, 0.012, -0.35);
    body.add(handguard);
    for (const side of [-1, 1]) {
      const vent = new Mesh(new BoxGeometry(0.012, 0.016, 0.19), darkMetal);
      vent.position.set(side * 0.049, 0.018, -0.35);
      body.add(vent);
    }
    for (let i = 0; i < 4; i += 1) {
      const rib = new Mesh(new BoxGeometry(0.102, 0.008, 0.018), darkMetal);
      rib.position.set(0, 0.061, -0.26 - i * 0.065);
      body.add(rib);
    }

    const barrel = new Mesh(new CylinderGeometry(0.017, 0.019, 0.34, 16), darkMetal);
    barrel.rotation.x = Math.PI / 2;
    barrel.position.set(0, 0.027, -0.58);
    body.add(barrel);
    const brake = new Mesh(new CylinderGeometry(0.033, 0.03, 0.09, 12), gunmetal);
    brake.rotation.x = Math.PI / 2;
    brake.position.set(0, 0.027, -0.76);
    body.add(brake);
    for (const side of [-1, 1]) {
      const port = new Mesh(new BoxGeometry(0.011, 0.02, 0.055), darkMetal);
      port.position.set(side * 0.025, 0.033, -0.76);
      body.add(port);
    }
    const muzzleRing = new Mesh(new TorusGeometry(0.032, 0.004, 5, 16), cyanGlow);
    muzzleRing.rotation.y = Math.PI / 2;
    muzzleRing.position.set(0, 0.027, -0.802);
    body.add(muzzleRing);
    const frontSight = new Mesh(new BoxGeometry(0.008, 0.035, 0.012), edge);
    frontSight.position.set(0, 0.085, -0.57);
    body.add(frontSight);
    const laser = new Mesh(new CylinderGeometry(0.014, 0.016, 0.11, 10), darkMetal);
    laser.rotation.x = Math.PI / 2;
    laser.position.set(-0.025, -0.04, -0.53);
    body.add(laser);
    const laserLens = new Mesh(new BoxGeometry(0.012, 0.012, 0.004), amberGlow);
    laserLens.position.set(-0.025, -0.04, -0.587);
    body.add(laserLens);

    const rail = new Mesh(new RoundedBoxGeometry(0.064, 0.018, 0.36, 2, 0.005), darkMetal);
    rail.position.set(0, 0.1, -0.18);
    body.add(rail);
    for (let i = 0; i < 9; i += 1) {
      const tooth = new Mesh(new BoxGeometry(0.07, 0.008, 0.018), gunmetal);
      tooth.position.set(0, 0.114, -0.33 + i * 0.036);
      body.add(tooth);
    }
    for (const side of [-1, 1]) {
      const topTrim = new Mesh(new BoxGeometry(0.004, 0.005, 0.32), edge);
      topTrim.position.set(side * 0.053, 0.062, -0.12);
      body.add(topTrim);
      const guardTrim = new Mesh(new BoxGeometry(0.004, 0.004, 0.3), edge);
      guardTrim.position.set(side * 0.047, -0.03, -0.34);
      body.add(guardTrim);
    }
    const chargeRail = new Mesh(new BoxGeometry(0.014, 0.006, 0.12), edge);
    chargeRail.position.set(0, 0.083, 0.03);
    body.add(chargeRail);

    const sightMount = new Mesh(new RoundedBoxGeometry(0.075, 0.028, 0.14, 2, 0.006), darkMetal);
    sightMount.position.set(0, 0.126, -0.075);
    body.add(sightMount);
    for (const side of [-1, 1]) {
      const frameSide = new Mesh(new BoxGeometry(0.008, 0.085, 0.045), gunmetal);
      frameSide.position.set(side * 0.031, 0.166, -0.11);
      body.add(frameSide);
    }
    for (const side of [-1, 1]) {
      const frameBar = new Mesh(new BoxGeometry(0.07, 0.007, 0.045), darkMetal);
      frameBar.position.set(0, 0.166 + side * 0.039, -0.11);
      body.add(frameBar);
    }
    const glass = new Mesh(
      new BoxGeometry(0.043, 0.061, 0.002),
      new MeshBasicMaterial({
        color: 0x63eaff,
        transparent: true,
        opacity: 0.2,
        depthWrite: false,
        blending: AdditiveBlending,
        side: DoubleSide,
      }),
    );
    glass.position.set(0, 0.168, -0.087);
    body.add(glass);
    const holoRing = new Mesh(new TorusGeometry(0.017, 0.0012, 4, 24), cyanGlow);
    holoRing.position.set(0, 0.168, -0.085);
    body.add(holoRing);
    const holoTickH = new Mesh(new BoxGeometry(0.034, 0.0012, 0.0012), cyanGlow);
    holoTickH.position.set(0, 0.168, -0.085);
    body.add(holoTickH);
    const holoTickV = new Mesh(new BoxGeometry(0.0012, 0.034, 0.0012), cyanGlow);
    holoTickV.position.set(0, 0.168, -0.085);
    body.add(holoTickV);
    const reticle = new Sprite(
      new SpriteMaterial({
        map: this.glowTexture,
        color: new Color(0xff4b6d),
        transparent: true,
        opacity: 0.95,
        depthWrite: false,
        blending: AdditiveBlending,
        toneMapped: false,
      }),
    );
    reticle.position.set(0, 0.168, -0.083);
    reticle.scale.setScalar(0.008);
    body.add(reticle);

    this.magazineMesh = new Mesh(new RoundedBoxGeometry(0.055, 0.13, 0.078, 3, 0.012), polymer);
    this.magazineMesh.position.set(0, -0.1, -0.025);
    this.magazineMesh.rotation.x = -0.2;
    body.add(this.magazineMesh);
    for (let i = 0; i < 3; i += 1) {
      const ridge = new Mesh(new BoxGeometry(0.058, 0.006, 0.082), darkMetal);
      ridge.position.set(0, -0.055 - i * 0.035, -0.02);
      ridge.rotation.x = -0.2;
      body.add(ridge);
    }

    const grip = new Mesh(new RoundedBoxGeometry(0.075, 0.16, 0.09, 3, 0.02), polymer);
    grip.position.set(0, -0.105, 0.09);
    grip.rotation.x = 0.28;
    body.add(grip);
    for (let i = 0; i < 5; i += 1) {
      const texture = new Mesh(new BoxGeometry(0.078, 0.006, 0.092), darkMetal);
      texture.position.set(0, -0.065 - i * 0.022, 0.095 + i * 0.006);
      texture.rotation.x = 0.28;
      body.add(texture);
    }

    const triggerGuard = new Mesh(new TorusGeometry(0.042, 0.006, 6, 16, Math.PI), darkMetal);
    triggerGuard.rotation.z = Math.PI;
    triggerGuard.position.set(0, -0.055, 0.018);
    body.add(triggerGuard);
    const trigger = new Mesh(new BoxGeometry(0.011, 0.045, 0.016), gunmetal);
    trigger.position.set(0, -0.055, 0.02);
    trigger.rotation.x = -0.2;
    body.add(trigger);

    const stockTop = new Mesh(new BoxGeometry(0.042, 0.022, 0.21), gunmetal);
    stockTop.position.set(0, 0.052, 0.215);
    stockTop.rotation.x = 0.035;
    body.add(stockTop);
    const stockBottom = new Mesh(new BoxGeometry(0.034, 0.018, 0.18), polymer);
    stockBottom.position.set(0, -0.052, 0.205);
    stockBottom.rotation.x = -0.1;
    body.add(stockBottom);
    const stockButt = new Mesh(new RoundedBoxGeometry(0.06, 0.12, 0.032, 2, 0.01), rubber);
    stockButt.position.set(0, 0, 0.327);
    stockButt.rotation.x = 0.02;
    body.add(stockButt);
    const rearPlate = new Mesh(new CylinderGeometry(0.042, 0.042, 0.016, 20), darkMetal);
    rearPlate.rotation.x = Math.PI / 2;
    rearPlate.position.set(0, 0.012, 0.13);
    body.add(rearPlate);

    const foregrip = new Mesh(new RoundedBoxGeometry(0.064, 0.14, 0.074, 3, 0.016), polymer);
    foregrip.position.set(0, -0.11, -0.31);
    foregrip.rotation.x = -0.22;
    body.add(foregrip);
    const foregripCap = new Mesh(new RoundedBoxGeometry(0.068, 0.018, 0.078, 2, 0.005), gunmetal);
    foregripCap.position.set(0, -0.18, -0.295);
    body.add(foregripCap);

    const sideAccent = new Mesh(new BoxGeometry(0.006, 0.012, 0.18), cyanGlow);
    sideAccent.position.set(-0.059, 0.045, -0.08);
    body.add(sideAccent);
    const sideAccent2 = new Mesh(new BoxGeometry(0.006, 0.008, 0.1), cyanGlow);
    sideAccent2.position.set(0.059, -0.025, -0.16);
    body.add(sideAccent2);
    const magazineLight = new Mesh(new BoxGeometry(0.05, 0.006, 0.005), magentaGlow);
    magazineLight.position.set(0, -0.205, -0.015);
    body.add(magazineLight);

    const chargingHandle = new Mesh(new BoxGeometry(0.02, 0.02, 0.075), gunmetal);
    chargingHandle.position.set(0.07, 0.06, -0.02);
    body.add(chargingHandle);
    const ejection = new Mesh(new BoxGeometry(0.022, 0.024, 0.07), darkMetal);
    ejection.position.set(0.061, 0.044, 0.03);
    body.add(ejection);

    const displayTexture = this.makeDisplayTexture();
    const display = new Mesh(
      new PlaneGeometry(0.135, 0.068),
      new MeshBasicMaterial({
        map: displayTexture,
        transparent: true,
        toneMapped: false,
        side: DoubleSide,
      }),
    );
    display.position.set(0.0595, 0.012, 0.045);
    display.rotation.y = Math.PI / 2;
    body.add(display);

    for (let i = 0; i < 3; i += 1) {
      const screw = new Mesh(new CylinderGeometry(0.004, 0.004, 0.004, 8), gunmetal);
      screw.rotation.z = Math.PI / 2;
      screw.position.set(0.059, -0.026, 0.06 - i * 0.04);
      body.add(screw);
    }

    const sideRail = new Mesh(new BoxGeometry(0.006, 0.014, 0.22), darkMetal);
    sideRail.position.set(0.059, -0.038, -0.05);
    body.add(sideRail);

    const orangePlate = new Mesh(new BoxGeometry(0.004, 0.045, 0.06), amberGlow);
    orangePlate.position.set(0.0605, 0.024, -0.175);
    body.add(orangePlate);

    for (const z of [-0.24, -0.31, -0.38]) {
      const slot = new Mesh(new BoxGeometry(0.01, 0.014, 0.035), darkMetal);
      slot.position.set(0.049, 0.02, z);
      body.add(slot);
    }

    const flashMaterial = new SpriteMaterial({
      map: this.glowTexture,
      color: new Color(0xffd39a),
      transparent: true,
      opacity: 0,
      depthWrite: false,
      blending: AdditiveBlending,
      toneMapped: false,
    });
    this.flash = new Sprite(flashMaterial);
    this.flash.position.copy(this.muzzle);
    this.flash.scale.setScalar(0.35);
    this.root.add(this.flash);

    this.flashLight = new PointLight(0xffc27a, 0, 2.2, 2);
    this.flashLight.position.copy(this.muzzle);
    this.root.add(this.flashLight);

    for (let i = 0; i < 2; i += 1) {
      const material = new SpriteMaterial({
        map: this.glowTexture,
        color: new Color(0x6f7a86),
        transparent: true,
        opacity: 0,
        depthWrite: false,
        blending: NormalBlending,
      });
      const sprite = new Sprite(material);
      sprite.position.copy(this.muzzle);
      sprite.scale.setScalar(0.15);
      this.root.add(sprite);
      this.muzzleSmoke.push(sprite);
    }

    this.addLights();
  }

  private addLights() {
    const ambient = new AmbientLight(0xb8c0c8, 1.0);
    this.scene.add(ambient);
    const key = new DirectionalLight(0xffeedd, 2.6);
    key.position.set(1.2, 2.4, 1.8);
    this.scene.add(key);
    const rim = new PointLight(0xff7d94, 2.6, 3.4, 2);
    rim.position.set(-0.8, 0.2, 0.2);
    this.scene.add(rim);
    const fill = new PointLight(0x6feaff, 2.6, 3, 2);
    fill.position.set(0.7, -0.1, -0.8);
    this.scene.add(fill);
  }

  private makeDisplayTexture() {
    const canvas = document.createElement('canvas');
    canvas.width = 256;
    canvas.height = 128;
    const context = canvas.getContext('2d');
    if (!context) {
      throw new Error('无法创建弹药显示屏');
    }
    context.fillStyle = 'rgba(2, 9, 12, 0.94)';
    context.fillRect(0, 0, 256, 128);
    context.strokeStyle = 'rgba(89, 239, 255, 0.8)';
    context.lineWidth = 2;
    context.strokeRect(6, 6, 244, 116);
    context.fillStyle = 'rgba(105, 247, 255, 0.95)';
    context.font = '700 68px "Arial Narrow", sans-serif';
    context.fillText('VK', 18, 84);
    context.fillStyle = 'rgba(180, 228, 238, 0.72)';
    context.font = '500 15px "Arial Narrow", sans-serif';
    context.fillText('PULSE // LIVE', 106, 50);
    context.fillStyle = 'rgba(105, 247, 255, 0.5)';
    context.fillRect(106, 65, 118, 3);
    context.fillRect(106, 76, 88, 3);
    context.fillRect(106, 87, 102, 3);
    const texture = new CanvasTexture(canvas);
    texture.colorSpace = SRGBColorSpace;
    return texture;
  }

  private makeGlowTexture() {
    const canvas = document.createElement('canvas');
    canvas.width = 128;
    canvas.height = 128;
    const context = canvas.getContext('2d');
    if (!context) {
      throw new Error('无法创建枪口光晕');
    }
    const gradient = context.createRadialGradient(64, 64, 0, 64, 64, 64);
    gradient.addColorStop(0, 'rgba(255,255,235,1)');
    gradient.addColorStop(0.2, 'rgba(255,206,120,0.9)');
    gradient.addColorStop(0.55, 'rgba(255,126,42,0.28)');
    gradient.addColorStop(1, 'rgba(0,0,0,0)');
    context.fillStyle = gradient;
    context.fillRect(0, 0, 128, 128);
    const texture = new CanvasTexture(canvas);
    texture.colorSpace = SRGBColorSpace;
    return texture;
  }

  reset() {
    this.ammo = MAGAZINE_SIZE;
    this.reserve = RESERVE_SIZE;
    this.reloading = false;
    this.reloadProgress = 0;
    this.cooldown = 0;
    this.flashTimer = 0;
    this.recoil = 0;
    this.ads = 0;
    this.previousFire = false;
    this.previousReload = false;
    this.reloadStep = 0;
    this.events = [];
    for (const shell of this.shells) {
      this.root.remove(shell.mesh);
      shell.mesh.geometry.dispose();
    }
    this.shells.length = 0;
  }

  startReload() {
    if (this.reloading || this.ammo >= MAGAZINE_SIZE || this.reserve <= 0) {
      return false;
    }
    this.reloading = true;
    this.reloadProgress = 0;
    this.reloadStep = 0;
    this.events.push('reload-start');
    return true;
  }

  update(dt: number, input: WeaponInput): WeaponUpdate {
    this.events = [];
    this.elapsed += dt;
    this.cooldown = Math.max(0, this.cooldown - dt);

    if (input.reload && !this.previousReload) {
      this.startReload();
    }
    this.previousReload = input.reload;

    if (this.reloading) {
      this.reloadProgress = clamp(this.reloadProgress + dt / RELOAD_DURATION, 0, 1);
      const steps = [0.22, 0.52, 0.78];
      if (this.reloadStep < steps.length && this.reloadProgress >= (steps[this.reloadStep] ?? 1)) {
        this.events.push('reload-step');
        this.reloadStep += 1;
      }
      if (this.reloadProgress >= 1) {
        const needed = MAGAZINE_SIZE - this.ammo;
        const loaded = Math.min(needed, this.reserve);
        this.ammo += loaded;
        this.reserve -= loaded;
        this.reloading = false;
        this.reloadProgress = 0;
        this.events.push('reload-end');
      }
    }

    let fired = false;
    if (input.fire && !this.reloading && this.cooldown <= 0) {
      if (this.ammo > 0) {
        this.ammo -= 1;
        this.cooldown = FIRE_INTERVAL;
        fired = true;
        this.recoil = Math.min(0.1, this.recoil + 0.026);
        this.flashTimer = 0.045;
        this.spawnShell();
      } else if (!this.previousFire) {
        this.events.push('empty');
      }
    }
    this.previousFire = input.fire;

    this.updateAnimation(dt, input, fired);

    return {
      fired,
      spread: this.getSpread(input),
    };
  }

  private getSpread(input: WeaponInput) {
    if (input.aim) {
      return 0.0016 + input.moving * 0.0016;
    }
    return 0.0085 + input.moving * 0.012 + (input.sprint ? 0.014 : 0);
  }

  private updateAnimation(dt: number, input: WeaponInput, fired: boolean) {
    this.ads = damp(this.ads, input.aim && !input.sprint ? 1 : 0, 11, dt);
    this.sprintAmount = damp(this.sprintAmount, input.sprint && !input.aim ? 1 : 0, 8, dt);
    this.recoil = damp(this.recoil, 0, 14, dt);
    this.swayX = damp(this.swayX, clamp(-input.lookX * 0.00012, -0.025, 0.025), 10, dt);
    this.swayY = damp(this.swayY, clamp(-input.lookY * 0.0001, -0.02, 0.02), 10, dt);

    if (input.moving > 0.1 && input.grounded) {
      this.bobTime += dt * lerp(8, 13.5, this.sprintAmount) * input.moving;
    }
    const bob = input.moving * (1 - this.ads * 0.72);
    const bobX = Math.sin(this.bobTime) * 0.008 * bob;
    const bobY = Math.abs(Math.cos(this.bobTime)) * 0.007 * bob;

    const position = new Vector3();
    position.lerpVectors(this.basePosition, this.adsPosition, this.ads);
    position.x += bobX + this.swayX * (1 - this.ads * 0.65);
    position.y += bobY + this.swayY * (1 - this.ads * 0.65);
    position.z += this.recoil * 0.52;
    position.y -= this.sprintAmount * 0.07;
    position.x += this.sprintAmount * 0.03;

    let reloadPose = 0;
    if (this.reloading) {
      const progress = this.reloadProgress;
      reloadPose = Math.sin(progress * Math.PI);
    }
    position.y -= reloadPose * 0.1;
    position.z += reloadPose * 0.04;

    const rotationX =
      this.baseRotation.x + this.recoil * 2.4 - this.sprintAmount * 0.18 + reloadPose * 0.15;
    const rotationY =
      this.baseRotation.y * (1 - this.ads) -
      this.swayX * 0.8 +
      this.sprintAmount * 0.3 +
      reloadPose * 0.3;
    const rotationZ =
      this.baseRotation.z +
      this.sprintAmount * 0.32 +
      this.swayY * 0.5 +
      reloadPose * 0.35 +
      Math.sin(this.elapsed * 1.3) * 0.002;
    this.root.position.copy(position);
    this.root.rotation.set(rotationX, rotationY, rotationZ);

    this.magazineMesh.position.y = -0.12 - reloadPose * 0.3;
    this.magazineMesh.rotation.z = reloadPose * 0.4;
    this.magazineMesh.visible = !(
      this.reloading &&
      this.reloadProgress > 0.3 &&
      this.reloadProgress < 0.72
    );

    if (fired) {
      this.flash.position.set(
        0.005,
        0.03 + randomRange(-0.008, 0.008),
        -0.8 + randomRange(-0.02, 0.02),
      );
      this.flash.scale.setScalar(randomRange(0.28, 0.48));
      this.flash.material.rotation = randomRange(0, Math.PI);
    }
    this.flashTimer = Math.max(0, this.flashTimer - dt);
    const flashMaterial = this.flash.material as SpriteMaterial;
    flashMaterial.opacity = this.flashTimer > 0 ? 1 : 0;
    this.flash.visible = this.flashTimer > 0;
    this.flashLight.intensity = this.flashTimer > 0 ? 42 : 0;

    for (const smoke of this.muzzleSmoke) {
      const material = smoke.material as SpriteMaterial;
      if (this.flashTimer > 0) {
        material.opacity = 0.22;
        smoke.position.copy(this.flash.position);
        smoke.scale.setScalar(0.16);
      } else {
        material.opacity = Math.max(0, material.opacity - dt * 1.6);
        smoke.scale.addScalar(dt * 0.9);
        smoke.position.z -= dt * 0.16;
        smoke.position.y += dt * 0.04;
      }
    }

    this.camera.fov = this.ads > 0.5 ? 46 : 58;
    this.camera.updateProjectionMatrix();

    for (let i = this.shells.length - 1; i >= 0; i -= 1) {
      const shell = this.shells[i];
      if (!shell) {
        continue;
      }
      shell.life -= dt;
      shell.velocity.y -= 9.8 * dt;
      shell.mesh.position.addScaledVector(shell.velocity, dt);
      shell.mesh.rotation.x += dt * 12;
      shell.mesh.rotation.z += dt * 7;
      if (shell.life <= 0 || shell.mesh.position.y < -0.7) {
        this.root.remove(shell.mesh);
        shell.mesh.geometry.dispose();
        this.shells.splice(i, 1);
      }
    }
  }

  private spawnShell() {
    const geometry = new CylinderGeometry(0.007, 0.008, 0.028, 6);
    const material = this.materials[4] ?? this.materials[0];
    const mesh = new Mesh(geometry, material);
    mesh.position.set(0.075, 0.05, -0.01);
    mesh.rotation.z = Math.PI / 2;
    this.root.add(mesh);
    this.shells.push({
      mesh,
      velocity: new Vector3(randomRange(0.95, 1.5), randomRange(0.8, 1.35), randomRange(0.1, 0.5)),
      life: 0.9,
    });
    if (this.shells.length > 14) {
      const oldest = this.shells.shift();
      if (oldest) {
        this.root.remove(oldest.mesh);
        oldest.mesh.geometry.dispose();
      }
    }
  }

  getMuzzleWorldPosition(target: Vector3) {
    return target.copy(this.muzzle).applyMatrix4(this.root.matrixWorld);
  }

  dispose() {
    this.scene.traverse((object) => {
      if (!(object instanceof Mesh) && !(object instanceof Sprite)) {
        return;
      }
      object.geometry?.dispose();
      const material = object.material;
      if (Array.isArray(material)) {
        for (const item of material) {
          item.dispose();
        }
      } else if (material) {
        material.dispose();
      }
    });
    this.glowTexture.dispose();
    this.flash.geometry.dispose();
    (this.flash.material as SpriteMaterial).dispose();
    this.scene.clear();
  }
}
