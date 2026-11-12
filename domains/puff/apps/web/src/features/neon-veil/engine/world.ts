import {
  AdditiveBlending,
  Box3,
  BoxGeometry,
  BufferGeometry,
  CatmullRomCurve3,
  Color,
  ConeGeometry,
  CylinderGeometry,
  DirectionalLight,
  DoubleSide,
  Float32BufferAttribute,
  FogExp2,
  Group,
  HemisphereLight,
  Mesh,
  MeshBasicMaterial,
  MeshPhysicalMaterial,
  MeshStandardMaterial,
  type Object3D,
  PlaneGeometry,
  PointLight,
  Points,
  PointsMaterial,
  type Scene,
  ShaderMaterial,
  SpotLight,
  Sprite,
  SpriteMaterial,
  type Texture,
  TorusGeometry,
  TubeGeometry,
  Vector3,
  type WebGLRenderer,
} from 'three';
import { Reflector } from 'three/addons/objects/Reflector.js';
import { MistSystem, RainSystem, RippleField } from './rain';
import {
  createBillboardTexture,
  createBuildingTexture,
  createConcreteTextures,
  createContactShadowTexture,
  createGlowTexture,
  createHazardTexture,
  createMetalTexture,
  createPuddleAlpha,
  createSmokeTexture,
} from './textures';
import type { QualityLevel } from './types';
import { randomRange } from './utils';

interface SteamPuff {
  sprite: Sprite;
  base: Vector3;
  phase: number;
  speed: number;
  scale: number;
}

interface Airship {
  group: Group;
  speed: number;
  range: number;
  phase: number;
}

const neonMaterial = (color: number, intensity = 1) =>
  new MeshBasicMaterial({
    color: new Color(color).multiplyScalar(intensity),
    toneMapped: false,
  });

export class World {
  readonly scene: Scene;
  readonly colliders: Box3[] = [];
  readonly occluders: Mesh[] = [];

  private readonly tickers: Array<(time: number, dt: number) => void> = [];
  private readonly steam: SteamPuff[] = [];
  private readonly airships: Airship[] = [];
  private readonly rain: RainSystem;
  private readonly ripples: RippleField;
  private readonly mist: MistSystem;
  private readonly glowTexture: Texture;
  private readonly smokeTexture: Texture;
  private readonly puddleAlpha: Texture;
  private readonly noiseTexture: Texture;
  private readonly contactShadow: Texture;
  private readonly contactShadowMaterial: MeshBasicMaterial;
  private readonly reactorLight: PointLight;
  private readonly neonLights: Array<{ light: PointLight; phase: number; base: number }> = [];
  private readonly optionalLights: PointLight[] = [];
  private menuLight: SpotLight | null = null;
  private menuWarm: PointLight | null = null;
  private floorReflector: Reflector | null = null;
  private floorOverlay!: Mesh;

  constructor(scene: Scene, renderer: WebGLRenderer, environment: Texture, quality: QualityLevel) {
    this.scene = scene;
    scene.fog = new FogExp2(0x05070d, 0.0155);
    scene.environment = environment;
    scene.environmentIntensity = 0.46;

    this.glowTexture = createGlowTexture();
    this.smokeTexture = createSmokeTexture();
    this.puddleAlpha = createPuddleAlpha();
    this.noiseTexture = createGlowTexture('rgba(255,255,255,0.9)');
    this.contactShadow = createContactShadowTexture();
    this.contactShadowMaterial = new MeshBasicMaterial({
      map: this.contactShadow,
      transparent: true,
      opacity: 0.55,
      depthWrite: false,
      toneMapped: false,
    });

    const concrete = createConcreteTextures();
    const metal = createMetalTexture();
    const hazard = createHazardTexture();

    const wallConcrete = new MeshStandardMaterial({
      map: concrete.map,
      bumpMap: concrete.bump,
      bumpScale: 0.035,
      roughnessMap: concrete.roughness,
      roughness: 0.78,
      metalness: 0.25,
      color: 0xb9b4aa,
      envMapIntensity: 0.7,
    });
    const darkConcrete = new MeshStandardMaterial({
      map: concrete.map,
      bumpMap: concrete.bump,
      bumpScale: 0.05,
      roughnessMap: concrete.roughness,
      roughness: 0.62,
      metalness: 0.42,
      color: 0x807b74,
      envMapIntensity: 0.8,
    });
    const metalMaterial = new MeshStandardMaterial({
      map: metal.map,
      bumpMap: metal.bump,
      bumpScale: 0.025,
      roughnessMap: metal.roughness,
      roughness: 0.36,
      metalness: 0.9,
      color: 0x9c9b99,
      envMapIntensity: 1.0,
    });
    const darkMetal = new MeshStandardMaterial({
      map: metal.map,
      roughness: 0.45,
      metalness: 0.85,
      color: 0x33353a,
      envMapIntensity: 0.9,
    });
    const rubber = new MeshStandardMaterial({
      color: 0x0a0c10,
      roughness: 0.86,
      metalness: 0.1,
    });
    const hazardMaterial = new MeshStandardMaterial({
      map: hazard,
      roughness: 0.52,
      metalness: 0.4,
      emissive: 0x2b1b00,
      emissiveIntensity: 0.45,
    });
    const cyan = neonMaterial(0x64f6ff, 1.7);
    const magenta = neonMaterial(0xff3f9a, 1.5);
    const amber = neonMaterial(0xffa23a, 1.3);

    const floorMap = concrete.map.clone();
    const floorBump = concrete.bump.clone();
    const floorRoughness = concrete.roughness.clone();
    for (const texture of [floorMap, floorBump, floorRoughness]) {
      texture.repeat.set(7, 7);
      texture.needsUpdate = true;
    }
    const floorMaterial = new MeshPhysicalMaterial({
      map: floorMap,
      bumpMap: floorBump,
      bumpScale: 0.045,
      roughnessMap: floorRoughness,
      roughness: 0.5,
      metalness: 0.42,
      color: 0x7a7d80,
      clearcoat: 0.75,
      clearcoatRoughness: 0.2,
      envMapIntensity: 0.9,
      transparent: true,
      alphaMap: this.puddleAlpha,
      depthWrite: false,
    });

    this.buildSky(scene);
    this.buildLighting(scene);
    this.reactorLight = this.buildReactor(scene, metalMaterial, darkMetal, cyan, magenta);
    const floor = this.buildFloor(scene, renderer, floorMaterial, hazard, quality);
    this.floorOverlay = floor.overlay;
    this.floorReflector = floor.reflector;
    this.buildPerimeter(scene, wallConcrete, darkConcrete, metalMaterial, cyan, magenta);
    this.buildProps(scene, metalMaterial, darkMetal, hazardMaterial, cyan, magenta, amber, rubber);
    this.buildGates(scene, metalMaterial, cyan, magenta);
    this.buildBillboards(scene, metalMaterial, cyan, magenta);
    this.buildCatwalk(scene, metalMaterial, darkMetal);
    this.buildSkyline(scene);
    this.buildAirships();
    this.buildDust(scene);

    this.rain = new RainSystem(quality === 'cinematic' ? 5600 : 2600);
    scene.add(this.rain.object);
    this.ripples = new RippleField(300, 50);
    scene.add(this.ripples.points);
    this.mist = new MistSystem(this.smokeTexture, 16, 40);
    for (const sprite of this.mist.group) {
      scene.add(sprite);
    }
    this.setQuality(quality);
  }

  private buildSky(scene: Scene) {
    const sky = new Mesh(
      new BoxGeometry(220, 140, 220),
      new ShaderMaterial({
        side: DoubleSide,
        depthWrite: false,
        uniforms: {
          uTime: { value: 0 },
        },
        vertexShader: `
          varying vec3 vWorld;
          void main() {
            vWorld = position;
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          }
        `,
        fragmentShader: `
          uniform float uTime;
          varying vec3 vWorld;
          float hash(vec2 p) {
            return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
          }
          float noise(vec2 p) {
            vec2 i = floor(p);
            vec2 f = fract(p);
            f = f * f * (3.0 - 2.0 * f);
            return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), f.x),
              mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), f.x), f.y);
          }
          float fbm(vec2 p) {
            float value = 0.0;
            float amplitude = 0.55;
            for (int i = 0; i < 4; i++) {
              value += noise(p) * amplitude;
              p *= 2.04;
              amplitude *= 0.5;
            }
            return value;
          }
          void main() {
            vec3 dir = normalize(vWorld);
            float horizon = smoothstep(-0.12, 0.62, dir.y);
            vec3 base = mix(vec3(0.008, 0.014, 0.026), vec3(0.018, 0.042, 0.075), horizon);
            float clouds = fbm(dir.xz * 4.0 + vec2(uTime * 0.004, 0.0));
            clouds = smoothstep(0.42, 0.82, clouds);
            vec3 cloudColor = mix(vec3(0.012, 0.025, 0.05), vec3(0.12, 0.18, 0.28), horizon);
            base = mix(base, cloudColor, clouds * 0.52);
            float glow = pow(max(0.0, dot(dir, normalize(vec3(-0.55, 0.24, -0.8)))), 18.0);
            base += vec3(0.18, 0.32, 0.55) * glow * 0.8;
            float stars = step(0.9985, hash(floor(dir.xz * 900.0))) * (1.0 - horizon);
            base += vec3(0.3, 0.45, 0.7) * stars * 0.7;
            gl_FragColor = vec4(base, 1.0);
          }
        `,
      }),
    );
    sky.frustumCulled = false;
    sky.renderOrder = -100;
    scene.add(sky);
    this.tickers.push((time) => {
      const material = sky.material as ShaderMaterial;
      material.uniforms.uTime.value = time;
    });

    const moonMaterial = new SpriteMaterial({
      map: this.glowTexture,
      color: new Color(0.7, 0.84, 1),
      transparent: true,
      opacity: 0.85,
      depthWrite: false,
      blending: AdditiveBlending,
    });
    const moon = new Sprite(moonMaterial);
    moon.position.set(-72, 48, -86);
    moon.scale.set(30, 30, 1);
    scene.add(moon);

    const moonCore = new Mesh(new BoxGeometry(2.8, 2.8, 2.8), neonMaterial(0xcfe9ff, 3.6));
    moonCore.position.copy(moon.position);
    moonCore.lookAt(0, 10, 0);
    scene.add(moonCore);
  }

  private buildLighting(scene: Scene) {
    scene.add(new HemisphereLight(0x4b6786, 0x050506, 0.28));

    const moonLight = new SpotLight(0x9ec8ff, 300, 90, 0.7, 0.55, 1.2);
    moonLight.position.set(-32, 40, -30);
    moonLight.target.position.set(0, 0, 0);
    moonLight.castShadow = false;
    scene.add(moonLight);
    scene.add(moonLight.target);

    const warmKey = new DirectionalLight(0xffb178, 2.55);
    warmKey.position.set(20, 13, 21);
    warmKey.target.position.set(0, 0, -2);
    warmKey.castShadow = true;
    warmKey.shadow.mapSize.set(2048, 2048);
    warmKey.shadow.camera.near = 1;
    warmKey.shadow.camera.far = 90;
    warmKey.shadow.camera.left = -32;
    warmKey.shadow.camera.right = 32;
    warmKey.shadow.camera.top = 32;
    warmKey.shadow.camera.bottom = -32;
    warmKey.shadow.bias = -0.00035;
    warmKey.shadow.normalBias = 0.035;
    scene.add(warmKey);
    scene.add(warmKey.target);

    const coolRim = new DirectionalLight(0x8bc7ff, 0.8);
    coolRim.position.set(-10, 8, -24);
    scene.add(coolRim);

    const spots: Array<{
      position: Vector3;
      target: Vector3;
      color: number;
      intensity: number;
      shadow: boolean;
    }> = [
      {
        position: new Vector3(-19, 12.5, -19),
        target: new Vector3(-2, 0, 2),
        color: 0x63d9ff,
        intensity: 560,
        shadow: true,
      },
      {
        position: new Vector3(19, 12.5, 19),
        target: new Vector3(2, 0, -2),
        color: 0xff4d9d,
        intensity: 420,
        shadow: true,
      },
      {
        position: new Vector3(-19, 11, 19),
        target: new Vector3(0, 0, -1),
        color: 0x61f5ec,
        intensity: 320,
        shadow: false,
      },
      {
        position: new Vector3(19, 11, -19),
        target: new Vector3(0, 0, 1),
        color: 0x9b7bff,
        intensity: 300,
        shadow: false,
      },
    ];

    for (const spec of spots) {
      const spot = new SpotLight(spec.color, spec.intensity, 48, 0.32, 0.62, 1.5);
      spot.position.copy(spec.position);
      spot.target.position.copy(spec.target);
      spot.castShadow = spec.shadow;
      if (spec.shadow) {
        spot.shadow.mapSize.set(1024, 1024);
        spot.shadow.camera.near = 2;
        spot.shadow.camera.far = 45;
        spot.shadow.bias = -0.0004;
        spot.shadow.normalBias = 0.03;
      }
      scene.add(spot);
      scene.add(spot.target);
      this.addLightShaft(scene, spec.position, spec.color, 2.6, spec.position.y);
    }

    this.menuLight = new SpotLight(0xc5e2ff, 1050, 38, 0.52, 0.82, 1.35);
    this.menuLight.position.set(0, 8.5, 17);
    this.menuLight.target.position.set(0, 2.2, 0);
    this.menuLight.castShadow = false;
    scene.add(this.menuLight);
    scene.add(this.menuLight.target);
    this.menuWarm = new PointLight(0xffb06a, 55, 18, 2);
    this.menuWarm.position.set(-7, 3.2, 8);
    scene.add(this.menuWarm);

    const frontWarm = new SpotLight(0xff9b5e, 350, 44, 0.56, 0.72, 1.4);
    frontWarm.position.set(0, 10.5, 23);
    frontWarm.target.position.set(0, 1.8, 1);
    frontWarm.castShadow = false;
    scene.add(frontWarm);
    scene.add(frontWarm.target);

    const fill = new PointLight(0x7bd8ff, 24, 20, 2);
    fill.position.set(-12, 4, -12);
    scene.add(fill);
    const fillMagenta = new PointLight(0xff3d8c, 22, 18, 2);
    fillMagenta.position.set(13, 3.5, 12);
    scene.add(fillMagenta);
    this.neonLights.push({ light: fill, phase: 0.2, base: 24 });
    this.neonLights.push({ light: fillMagenta, phase: 1.8, base: 22 });

    const signCyan = new PointLight(0x5ee6ff, 30, 16, 2);
    signCyan.position.set(-8, 6, -19);
    scene.add(signCyan);
    const signMagenta = new PointLight(0xff4f9c, 26, 14, 2);
    signMagenta.position.set(19, 5, -2);
    scene.add(signMagenta);
    this.neonLights.push({ light: signCyan, phase: 3.1, base: 30 });
    this.optionalLights.push(signCyan);
    this.neonLights.push({ light: signMagenta, phase: 4.4, base: 26 });
    this.optionalLights.push(signMagenta);

    const sodiumA = new PointLight(0xff7a32, 26, 11, 2);
    sodiumA.position.set(12.8, 2.4, 7.7);
    scene.add(sodiumA);
    const sodiumB = new PointLight(0xff8c46, 22, 10, 2);
    sodiumB.position.set(-14.2, 2.1, 6.9);
    scene.add(sodiumB);
    this.neonLights.push({ light: sodiumA, phase: 2.2, base: 26 });
    this.neonLights.push({ light: sodiumB, phase: 4.9, base: 22 });
  }

  private addLightShaft(
    scene: Scene,
    position: Vector3,
    color: number,
    radius: number,
    height: number,
  ) {
    const geometry = new ConeGeometry(radius, height, 40, 1, true);
    const material = new ShaderMaterial({
      uniforms: {
        uColor: { value: new Color(color) },
        uTime: { value: 0 },
        uOpacity: { value: 0.052 },
        uHeight: { value: height },
      },
      vertexShader: `
        uniform float uHeight;
        varying vec2 vUv;
        varying float vHeight;
        void main() {
          vUv = uv;
          vHeight = position.y / uHeight + 0.5;
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
      `,
      fragmentShader: `
        uniform vec3 uColor;
        uniform float uTime;
        uniform float uOpacity;
        varying vec2 vUv;
        varying float vHeight;
        void main() {
          float vertical = pow(clamp(vHeight, 0.0, 1.0), 1.7);
          float bands = 0.76 + 0.24 * sin(vUv.y * 34.0 + uTime * 0.5);
          float edge = smoothstep(0.0, 0.32, vUv.x) * smoothstep(1.0, 0.68, vUv.x);
          float alpha = uOpacity * vertical * bands * (0.35 + 0.65 * edge);
          if (alpha < 0.004) discard;
          gl_FragColor = vec4(uColor, alpha);
        }
      `,
      transparent: true,
      depthWrite: false,
      side: DoubleSide,
      blending: AdditiveBlending,
    });
    const cone = new Mesh(geometry, material);
    cone.position.copy(position);
    cone.position.y -= height / 2;
    cone.renderOrder = 10;
    scene.add(cone);
    this.tickers.push((time) => {
      material.uniforms.uTime.value = time;
    });
  }

  private buildFloor(
    scene: Scene,
    renderer: WebGLRenderer,
    material: MeshPhysicalMaterial,
    hazard: Texture,
    quality: QualityLevel,
  ) {
    const underlay = new Mesh(
      new PlaneGeometry(140, 140),
      new MeshStandardMaterial({ color: 0x03050a, roughness: 0.9 }),
    );
    underlay.rotation.x = -Math.PI / 2;
    underlay.position.y = -0.05;
    scene.add(underlay);

    const reflector = new Reflector(new PlaneGeometry(54, 54), {
      textureWidth: quality === 'cinematic' ? 1024 : 512,
      textureHeight: quality === 'cinematic' ? 1024 : 512,
      color: new Color(0x0b1017),
      clipBias: 0.0025,
    });
    reflector.rotation.x = -Math.PI / 2;
    reflector.position.y = 0;
    reflector.renderOrder = -2;
    scene.add(reflector);

    const overlay = new Mesh(new PlaneGeometry(54, 54), material);
    overlay.rotation.x = -Math.PI / 2;
    overlay.position.y = 0.018;
    overlay.receiveShadow = true;
    overlay.renderOrder = 2;
    scene.add(overlay);

    const hazardTexture = hazard.clone();
    hazardTexture.needsUpdate = true;
    const hazardStrip = new Mesh(
      new PlaneGeometry(54, 0.45),
      new MeshStandardMaterial({
        map: hazardTexture,
        roughness: 0.6,
        metalness: 0.4,
      }),
    );
    hazardStrip.rotation.x = -Math.PI / 2;
    hazardStrip.position.set(0, 0.03, -21.5);
    scene.add(hazardStrip);

    void renderer;
    return { overlay, reflector };
  }

  private buildPerimeter(
    scene: Scene,
    concrete: MeshStandardMaterial,
    darkConcrete: MeshStandardMaterial,
    metal: MeshStandardMaterial,
    cyan: MeshBasicMaterial,
    magenta: MeshBasicMaterial,
  ) {
    const walls: Array<[number, number, number, number]> = [
      [0, -26, 54, 1.2],
      [0, 26, 54, 1.2],
      [-26, 0, 1.2, 54],
      [26, 0, 1.2, 54],
    ];
    for (const [x, z, width, depth] of walls) {
      const wall = new Mesh(new BoxGeometry(width, 9, depth), concrete);
      wall.position.set(x, 4.5, z);
      wall.castShadow = true;
      wall.receiveShadow = true;
      scene.add(wall);
      this.addCollider(wall);
      this.occluders.push(wall);

      const trim = new Mesh(new BoxGeometry(width * 0.99, 0.32, depth * 1.15), darkConcrete);
      trim.position.set(x, 0.5, z);
      scene.add(trim);

      const neon = new Mesh(new BoxGeometry(width * 0.96, 0.045, depth * 1.2), cyan);
      neon.position.set(x, 4.9, z);
      scene.add(neon);
      const lowerNeon = new Mesh(new BoxGeometry(width * 0.9, 0.025, depth * 1.2), magenta);
      lowerNeon.position.set(x, 1.1, z);
      scene.add(lowerNeon);
    }

    for (let i = -24; i <= 24; i += 6) {
      if (Math.abs(i) > 18) {
        continue;
      }
      for (const z of [-25.2, 25.2]) {
        const rib = new Mesh(new BoxGeometry(0.48, 8.4, 0.5), metal);
        rib.position.set(i, 4.2, z);
        rib.castShadow = true;
        scene.add(rib);
      }
      for (const x of [-25.2, 25.2]) {
        const rib = new Mesh(new BoxGeometry(0.5, 8.4, 0.48), metal);
        rib.position.set(x, 4.2, i);
        rib.castShadow = true;
        scene.add(rib);
      }
    }

    for (const y of [6.4, 6.9, 7.35]) {
      for (const z of [-25.45, 25.45]) {
        const pipe = new Mesh(new CylinderGeometry(0.09, 0.09, 46, 10), metal);
        pipe.rotation.z = Math.PI / 2;
        pipe.position.set(0, y, z);
        scene.add(pipe);
      }
      for (const x of [-25.45, 25.45]) {
        const pipe = new Mesh(new CylinderGeometry(0.09, 0.09, 46, 10), metal);
        pipe.rotation.x = Math.PI / 2;
        pipe.position.set(x, y, 0);
        scene.add(pipe);
      }
    }

    for (let i = -20; i <= 20; i += 10) {
      const cable = new Mesh(
        new TubeGeometry(
          new CatmullRomCurve3([
            new Vector3(i - 6, 8.5, -25.2),
            new Vector3(i - 2, 7.2, -24.1),
            new Vector3(i + 2, 7.9, -25.1),
            new Vector3(i + 6, 6.6, -24.7),
          ]),
          28,
          0.035,
          6,
        ),
        new MeshStandardMaterial({ color: 0x080a0d, roughness: 0.95 }),
      );
      scene.add(cable);
    }
  }

  private buildReactor(
    scene: Scene,
    metal: MeshStandardMaterial,
    darkMetal: MeshStandardMaterial,
    cyan: MeshBasicMaterial,
    magenta: MeshBasicMaterial,
  ) {
    const group = new Group();
    scene.add(group);

    this.addContactShadow(group, 13, 13, 0.025);
    const platform = new Mesh(new CylinderGeometry(5.1, 5.3, 0.22, 64), darkMetal);
    platform.position.y = 0.11;
    platform.receiveShadow = true;
    group.add(platform);

    const collar = new Mesh(new CylinderGeometry(4.15, 4.3, 0.65, 48), metal);
    collar.position.y = 0.42;
    collar.castShadow = true;
    collar.receiveShadow = true;
    group.add(collar);

    const base = new Mesh(new CylinderGeometry(3.2, 3.7, 1.3, 32), darkMetal);
    base.position.y = 1.2;
    base.castShadow = true;
    group.add(base);

    const body = new Mesh(new CylinderGeometry(2.45, 2.8, 2.2, 32, 1, true), metal);
    body.position.y = 2.7;
    body.castShadow = true;
    group.add(body);

    const core = new Mesh(new CylinderGeometry(1.08, 1.08, 3.1, 32, 1, true), cyan);
    core.position.y = 2.5;
    group.add(core);
    const coreInner = new Mesh(
      new CylinderGeometry(0.55, 0.55, 3.05, 24),
      neonMaterial(0xcaf7ff, 1.8),
    );
    coreInner.position.y = 2.5;
    group.add(coreInner);

    const cap = new Mesh(new CylinderGeometry(3.2, 2.55, 0.58, 32), metal);
    cap.position.y = 4.02;
    cap.castShadow = true;
    group.add(cap);

    const topGlow = new Mesh(new CylinderGeometry(1.35, 1.35, 0.2, 32), magenta);
    topGlow.position.y = 4.35;
    group.add(topGlow);

    for (const [index, radius] of [2.95, 3.15, 3.4].entries()) {
      const ring = new Mesh(new TorusGeometry(radius, 0.055, 8, 64), index === 1 ? magenta : cyan);
      ring.rotation.x = Math.PI / 2;
      ring.position.y = 1.65 + index * 0.72;
      group.add(ring);
      this.tickers.push((time) => {
        ring.rotation.z = time * (0.18 + index * 0.06) * (index % 2 === 0 ? 1 : -1);
      });
    }

    for (let i = 0; i < 4; i += 1) {
      const angle = (i / 4) * Math.PI * 2 + Math.PI / 4;
      const x = Math.cos(angle) * 3.1;
      const z = Math.sin(angle) * 3.1;
      const pylon = new Mesh(new BoxGeometry(0.38, 3.4, 0.38), metal);
      pylon.position.set(x, 1.9, z);
      pylon.rotation.y = -angle;
      pylon.castShadow = true;
      group.add(pylon);
      const pylonLight = new Mesh(new BoxGeometry(0.06, 2.2, 0.06), i % 2 === 0 ? cyan : magenta);
      pylonLight.position.set(x * 1.03, 1.9, z * 1.03);
      pylonLight.rotation.y = -angle;
      group.add(pylonLight);
    }

    const ringLight = new PointLight(0x5ff4ff, 28, 18, 2);
    ringLight.position.set(0, 2.6, 0);
    group.add(ringLight);
    const topLight = new PointLight(0xff59a8, 22, 9, 2);
    topLight.position.set(0, 4.5, 0);
    group.add(topLight);

    const beamMaterial = new ShaderMaterial({
      uniforms: {
        uTime: { value: 0 },
        uColor: { value: new Color(0.3, 0.92, 1) },
      },
      vertexShader: `
        varying vec2 vUv;
        void main() {
          vUv = uv;
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
      `,
      fragmentShader: `
        uniform float uTime;
        uniform vec3 uColor;
        varying vec2 vUv;
        void main() {
          float fade = pow(1.0 - vUv.y, 2.2);
          float edge = smoothstep(0.0, 0.35, vUv.x) * smoothstep(1.0, 0.65, vUv.x);
          float pulse = 0.76 + 0.24 * sin(uTime * 1.3 + vUv.y * 9.0);
          gl_FragColor = vec4(uColor, fade * edge * pulse * 0.055);
        }
      `,
      transparent: true,
      depthWrite: false,
      side: DoubleSide,
      blending: AdditiveBlending,
    });
    const beam = new Mesh(new CylinderGeometry(1.3, 3.7, 11, 32, 1, true), beamMaterial);
    beam.position.y = 7.5;
    group.add(beam);
    this.tickers.push((time) => {
      beamMaterial.uniforms.uTime.value = time;
    });

    for (const mesh of [platform, collar, base, body, cap, coreInner, topGlow]) {
      this.occluders.push(mesh);
    }
    this.addBoxCollider(0, 2.25, 0, 7.4, 4.5, 7.4);

    for (let i = 0; i < 9; i += 1) {
      const material = new SpriteMaterial({
        map: this.smokeTexture,
        color: new Color(0.48, 0.68, 0.8),
        transparent: true,
        opacity: 0.13,
        depthWrite: false,
        blending: AdditiveBlending,
      });
      const sprite = new Sprite(material);
      const basePosition = new Vector3(randomRange(-0.4, 0.4), 4.6, randomRange(-0.4, 0.4));
      sprite.position.copy(basePosition);
      sprite.scale.setScalar(randomRange(1.2, 2.4));
      group.add(sprite);
      this.steam.push({
        sprite,
        base: basePosition,
        phase: Math.random(),
        speed: randomRange(0.12, 0.24),
        scale: randomRange(1.4, 2.8),
      });
    }

    return ringLight;
  }

  private buildGates(
    scene: Scene,
    metal: MeshStandardMaterial,
    cyan: MeshBasicMaterial,
    magenta: MeshBasicMaterial,
  ) {
    const gates: Array<{ x: number; z: number; rotation: number; color: MeshBasicMaterial }> = [
      { x: 0, z: -24.3, rotation: 0, color: magenta },
      { x: 0, z: 24.3, rotation: Math.PI, color: cyan },
      { x: -24.3, z: 0, rotation: Math.PI / 2, color: cyan },
      { x: 24.3, z: 0, rotation: -Math.PI / 2, color: magenta },
    ];

    for (const gate of gates) {
      const group = new Group();
      group.position.set(gate.x, 0, gate.z);
      group.rotation.y = gate.rotation;
      scene.add(group);
      for (const side of [-1, 1]) {
        const pillar = new Mesh(new BoxGeometry(0.46, 4.4, 0.62), metal);
        pillar.position.set(side * 1.85, 2.2, 0);
        pillar.castShadow = true;
        group.add(pillar);
        const strip = new Mesh(new BoxGeometry(0.08, 3.6, 0.7), gate.color);
        strip.position.set(side * 1.85, 2.15, 0.02);
        group.add(strip);
      }
      const header = new Mesh(new BoxGeometry(4.2, 0.46, 0.62), metal);
      header.position.y = 4.38;
      header.castShadow = true;
      group.add(header);
      const headerStrip = new Mesh(new BoxGeometry(3.4, 0.08, 0.68), gate.color);
      headerStrip.position.y = 4.38;
      group.add(headerStrip);

      const ring = new Mesh(new TorusGeometry(1.5, 0.035, 8, 48), gate.color);
      ring.rotation.x = Math.PI / 2;
      ring.position.y = 0.065;
      group.add(ring);
      this.tickers.push((time) => {
        const pulse = 0.65 + Math.sin(time * 2.1 + gate.x) * 0.35;
        ring.scale.setScalar(0.94 + pulse * 0.08);
      });

      const light = new PointLight(gate.color.color.getHex(), 16, 10, 2);
      light.position.set(0, 2.2, 0.8);
      group.add(light);
      this.neonLights.push({ light, phase: gate.x + gate.z, base: 16 });
      this.optionalLights.push(light);
    }
  }

  private buildProps(
    scene: Scene,
    metal: MeshStandardMaterial,
    darkMetal: MeshStandardMaterial,
    hazard: MeshStandardMaterial,
    cyan: MeshBasicMaterial,
    magenta: MeshBasicMaterial,
    amber: MeshBasicMaterial,
    rubber: MeshStandardMaterial,
  ) {
    const crates: Array<[number, number, number, number]> = [
      [6.2, 9.3, 0, 1],
      [8.1, 9.4, Math.PI / 2, 1],
      [-7.3, 8.7, Math.PI / 2, 1.1],
      [-9.2, 7.5, 0, 0.95],
      [10.7, 2.2, 0, 1.05],
      [-11.4, 2.8, Math.PI / 2, 1],
      [5.8, -8.6, 0, 1.1],
      [-5.4, -9.4, Math.PI / 2, 1],
      [-6.9, -8.2, 0, 0.92],
      [13.8, -5.4, Math.PI / 2, 0.98],
      [-13.2, -4.3, 0, 1.05],
      [2.6, 13.2, 0, 0.92],
      [-2.9, 14.3, Math.PI / 2, 1.02],
    ];
    for (const [x, z, rotation, scale] of crates) {
      this.createCrate(scene, x, z, rotation, scale, metal, darkMetal, hazard, amber);
    }

    const barriers: Array<[number, number, number]> = [
      [3.4, 5.2, 0],
      [-3.8, 5.7, 0],
      [6.7, -3.7, Math.PI / 2],
      [-7.1, -3.3, Math.PI / 2],
      [11.9, 9.5, 0.3],
      [-12.7, 9.8, -0.25],
    ];
    for (const [x, z, rotation] of barriers) {
      this.createBarrier(scene, x, z, rotation, metal, hazard, cyan);
    }

    for (const [x, z] of [
      [-3.3, -12.4],
      [3.8, -13.2],
      [12.8, 7.7],
      [-14.2, 6.9],
      [15.7, 12.9],
      [-16.2, -13.5],
    ]) {
      this.createBarrel(scene, x, z, metal, amber, magenta);
    }

    for (let i = 0; i < 4; i += 1) {
      const pipe = new Mesh(new CylinderGeometry(0.34, 0.34, 1.5, 18, 1, true), metal);
      pipe.position.set(-17.7 - i * 0.9, 0.78, -18.5);
      pipe.castShadow = true;
      scene.add(pipe);
    }

    const duct = new Mesh(new BoxGeometry(1.2, 1.05, 3.8), darkMetal);
    duct.position.set(18.8, 3.1, -19.4);
    duct.rotation.y = 0.1;
    duct.castShadow = true;
    scene.add(duct);
    this.addCollider(duct);

    const vent = new Mesh(new BoxGeometry(0.32, 2.2, 1.4), rubber);
    vent.position.set(21.4, 2.3, -19.4);
    scene.add(vent);

    const cable = new Mesh(
      new TubeGeometry(
        new CatmullRomCurve3([
          new Vector3(-18, 7.6, 14),
          new Vector3(-12, 6.4, 12),
          new Vector3(-6, 7.2, 14),
          new Vector3(0, 5.9, 13.4),
        ]),
        40,
        0.055,
        6,
      ),
      rubber,
    );
    scene.add(cable);
  }

  private createCrate(
    scene: Scene,
    x: number,
    z: number,
    rotation: number,
    scale: number,
    metal: MeshStandardMaterial,
    darkMetal: MeshStandardMaterial,
    hazard: MeshStandardMaterial,
    amber: MeshBasicMaterial,
  ) {
    const group = new Group();
    group.position.set(x, 0, z);
    group.rotation.y = rotation;
    group.scale.setScalar(scale);
    scene.add(group);

    this.addContactShadow(group, 2.7, 2.5, 0.028);
    const body = new Mesh(new BoxGeometry(1.72, 1.18, 1.52), metal);
    body.position.y = 0.63;
    body.castShadow = true;
    body.receiveShadow = true;
    group.add(body);

    for (const side of [-1, 1]) {
      const band = new Mesh(new BoxGeometry(1.8, 0.09, 1.6), darkMetal);
      band.position.y = 0.32 + (side + 1) * 0.36;
      group.add(band);
      const rail = new Mesh(new BoxGeometry(0.1, 1.1, 1.58), darkMetal);
      rail.position.set(side * 0.82, 0.63, 0);
      group.add(rail);
    }

    const plate = new Mesh(new PlaneGeometry(1.2, 0.3), hazard);
    plate.position.set(0, 0.63, 0.765);
    group.add(plate);
    const marker = new Mesh(new BoxGeometry(0.52, 0.045, 0.02), amber);
    marker.position.set(0, 1.12, 0.765);
    group.add(marker);
    const top = new Mesh(new BoxGeometry(0.7, 0.06, 0.7), darkMetal);
    top.position.y = 1.23;
    group.add(top);

    group.updateMatrixWorld(true);
    this.colliders.push(new Box3().setFromObject(group));
    group.traverse((child) => {
      if (child instanceof Mesh) {
        this.occluders.push(child);
      }
    });
  }

  private createBarrier(
    scene: Scene,
    x: number,
    z: number,
    rotation: number,
    metal: MeshStandardMaterial,
    hazard: MeshStandardMaterial,
    cyan: MeshBasicMaterial,
  ) {
    const group = new Group();
    group.position.set(x, 0, z);
    group.rotation.y = rotation;
    scene.add(group);

    this.addContactShadow(group, 3.4, 1.5, 0.028);
    const body = new Mesh(new BoxGeometry(2.45, 0.9, 0.36), metal);
    body.position.y = 0.55;
    body.castShadow = true;
    body.receiveShadow = true;
    group.add(body);
    const base = new Mesh(new BoxGeometry(2.6, 0.2, 0.7), metal);
    base.position.y = 0.11;
    group.add(base);
    const stripe = new Mesh(new PlaneGeometry(2.1, 0.22), hazard);
    stripe.position.set(0, 0.66, 0.185);
    group.add(stripe);
    const light = new Mesh(new BoxGeometry(1.85, 0.035, 0.02), cyan);
    light.position.set(0, 1.01, 0.19);
    group.add(light);

    group.updateMatrixWorld(true);
    this.colliders.push(new Box3().setFromObject(group));
    group.traverse((child) => {
      if (child instanceof Mesh) {
        this.occluders.push(child);
      }
    });
  }

  private createBarrel(
    scene: Scene,
    x: number,
    z: number,
    metal: MeshStandardMaterial,
    amber: MeshBasicMaterial,
    magenta: MeshBasicMaterial,
  ) {
    const group = new Group();
    group.position.set(x, 0, z);
    scene.add(group);
    this.addContactShadow(group, 1.5, 1.5, 0.028);
    const body = new Mesh(new CylinderGeometry(0.42, 0.46, 1.15, 18), metal);
    body.position.y = 0.58;
    body.castShadow = true;
    body.receiveShadow = true;
    group.add(body);
    for (const y of [0.3, 0.88]) {
      const ring = new Mesh(new TorusGeometry(0.43, 0.035, 8, 24), metal);
      ring.rotation.x = Math.PI / 2;
      ring.position.y = y;
      group.add(ring);
    }
    const band = new Mesh(new BoxGeometry(0.58, 0.04, 0.02), amber);
    band.position.set(0, 0.72, 0.43);
    group.add(band);
    const marker = new Mesh(new BoxGeometry(0.12, 0.32, 0.02), magenta);
    marker.position.set(0.28, 0.52, 0.42);
    marker.rotation.z = -0.3;
    group.add(marker);

    group.updateMatrixWorld(true);
    this.colliders.push(new Box3().setFromObject(group));
    group.traverse((child) => {
      if (child instanceof Mesh) {
        this.occluders.push(child);
      }
    });
  }

  private buildBillboards(
    scene: Scene,
    metal: MeshStandardMaterial,
    cyan: MeshBasicMaterial,
    magenta: MeshBasicMaterial,
  ) {
    const boards: Array<{
      title: string;
      subtitle: string;
      accent: string;
      position: Vector3;
      rotation: number;
      width: number;
      height: number;
      light: number;
    }> = [
      {
        title: 'SECTOR 07',
        subtitle: '第七区 · 反应堆禁入',
        accent: '#63ecff',
        position: new Vector3(-9.5, 5.6, -25.05),
        rotation: 0,
        width: 9.2,
        height: 4.2,
        light: 0x5fe8ff,
      },
      {
        title: 'NO SIGNAL',
        subtitle: '失去联络 · 保持警戒',
        accent: '#ff4c9e',
        position: new Vector3(25.05, 5.1, 5.5),
        rotation: -Math.PI / 2,
        width: 7.4,
        height: 3.5,
        light: 0xff4c9e,
      },
    ];

    for (const board of boards) {
      const backing = new Mesh(new BoxGeometry(board.width + 0.3, board.height + 0.3, 0.16), metal);
      backing.position.copy(board.position);
      backing.rotation.y = board.rotation;
      scene.add(backing);

      const texture = createBillboardTexture(board.title, board.subtitle, board.accent);
      const material = new MeshBasicMaterial({
        map: texture,
        transparent: true,
        toneMapped: false,
        side: DoubleSide,
      });
      const plane = new Mesh(new PlaneGeometry(board.width, board.height), material);
      plane.position.copy(board.position);
      plane.position.y += 0.02;
      plane.rotation.y = board.rotation;
      plane.translateZ(0.1);
      scene.add(plane);

      const light = new PointLight(board.light, 24, 15, 2);
      light.position.copy(board.position);
      light.position.y -= 1;
      scene.add(light);
      this.neonLights.push({ light, phase: Math.random() * 5, base: 24 });
      this.optionalLights.push(light);

      this.tickers.push((time) => {
        const flicker =
          Math.sin(time * 7.3 + board.position.x) > 0.93
            ? 0.55
            : 0.92 + Math.sin(time * 1.8 + board.position.z) * 0.08;
        material.opacity = flicker;
      });
    }

    const strip = new Mesh(new BoxGeometry(0.06, 4.2, 0.06), magenta);
    strip.position.set(-17.5, 5.6, -24.6);
    scene.add(strip);
    const cyanStrip = new Mesh(new BoxGeometry(0.06, 3.2, 0.06), cyan);
    cyanStrip.position.set(17.7, 4.8, 24.6);
    scene.add(cyanStrip);
  }

  private buildCatwalk(scene: Scene, metal: MeshStandardMaterial, darkMetal: MeshStandardMaterial) {
    const bridge = new Mesh(new BoxGeometry(28, 0.24, 2.4), darkMetal);
    bridge.position.set(0, 7.6, -6.5);
    bridge.castShadow = true;
    scene.add(bridge);

    for (const side of [-1, 1]) {
      const rail = new Mesh(new BoxGeometry(28, 0.1, 0.1), metal);
      rail.position.set(0, 8.65, -6.5 + side * 1.1);
      scene.add(rail);
      const lowerRail = new Mesh(new BoxGeometry(28, 0.05, 0.05), neonMaterial(0x45d6e4, 0.9));
      lowerRail.position.set(0, 8.05, -6.5 + side * 1.1);
      scene.add(lowerRail);
      for (let i = -13; i <= 13; i += 2) {
        const post = new Mesh(new BoxGeometry(0.06, 1.05, 0.06), metal);
        post.position.set(i, 8.1, -6.5 + side * 1.1);
        scene.add(post);
      }
    }

    for (const x of [-14, 14]) {
      const support = new Mesh(new BoxGeometry(0.5, 7.6, 0.5), metal);
      support.position.set(x, 3.8, -6.5);
      support.castShadow = true;
      scene.add(support);
      this.addCollider(support);
    }

    const overhead = new Mesh(new CylinderGeometry(0.28, 0.28, 22, 16), metal);
    overhead.rotation.z = Math.PI / 2;
    overhead.position.set(0, 9.4, -10.2);
    scene.add(overhead);
  }

  private buildSkyline(scene: Scene) {
    const group = new Group();
    scene.add(group);
    const texture = createBuildingTexture();
    const material = new MeshStandardMaterial({
      map: texture,
      emissiveMap: texture,
      emissive: new Color(0.55, 0.78, 1),
      emissiveIntensity: 0.82,
      color: 0x060910,
      roughness: 0.74,
      metalness: 0.35,
    });
    const accent = neonMaterial(0x48cfff, 1.2);
    const accentPink = neonMaterial(0xff3f90, 1.2);

    for (let i = 0; i < 30; i += 1) {
      const angle = (i / 30) * Math.PI * 2 + randomRange(-0.08, 0.08);
      const radius = randomRange(38, 74);
      const width = randomRange(4, 9);
      const depth = randomRange(4, 9);
      const height = randomRange(12, 44);
      const building = new Mesh(new BoxGeometry(width, height, depth), material);
      building.position.set(Math.cos(angle) * radius, height / 2 - 1.5, Math.sin(angle) * radius);
      group.add(building);

      if (i % 3 === 0) {
        const antenna = new Mesh(
          new CylinderGeometry(0.05, 0.05, randomRange(3, 8), 6),
          new MeshBasicMaterial({ color: 0x151d28 }),
        );
        antenna.position.set(building.position.x, height + 1.5, building.position.z);
        group.add(antenna);
        const beacon = new Mesh(
          new BoxGeometry(0.22, 0.22, 0.22),
          i % 2 === 0 ? accent : accentPink,
        );
        beacon.position.set(antenna.position.x, height + 5.4, antenna.position.z);
        group.add(beacon);
        this.tickers.push((time) => {
          beacon.visible = Math.sin(time * 2.2 + i) > -0.2;
        });
      }
    }

    const distantBridge = new Mesh(
      new BoxGeometry(90, 0.5, 1.5),
      new MeshStandardMaterial({
        color: 0x111723,
        emissive: new Color(0.1, 0.2, 0.3),
        emissiveIntensity: 0.6,
      }),
    );
    distantBridge.position.set(0, 17, -51);
    group.add(distantBridge);
  }

  private buildAirships() {
    for (let i = 0; i < 2; i += 1) {
      const group = new Group();
      const hull = new Mesh(
        new BoxGeometry(9, 2.2, 3.6),
        new MeshStandardMaterial({
          color: 0x06090e,
          roughness: 0.8,
          metalness: 0.4,
          emissive: new Color(0.02, 0.06, 0.1),
        }),
      );
      group.add(hull);
      const belly = new Mesh(new BoxGeometry(6.5, 0.05, 2.8), neonMaterial(0xff4b9a, 1.4));
      belly.position.y = -1.12;
      group.add(belly);
      const cabin = new Mesh(
        new BoxGeometry(2.2, 0.9, 2.2),
        new MeshStandardMaterial({
          color: 0x0b0f16,
          emissive: new Color(0.2, 0.45, 0.7),
          emissiveIntensity: 0.55,
        }),
      );
      cabin.position.set(-2.4, -1.3, 0);
      group.add(cabin);
      const beacon = new Mesh(new BoxGeometry(0.18, 0.18, 0.18), neonMaterial(0xff3333, 2));
      beacon.position.set(4.4, 1.2, 0);
      group.add(beacon);
      this.tickers.push((time) => {
        beacon.visible = Math.sin(time * 3.6 + i * 2) > 0.1;
      });
      group.position.set(0, 16 + i * 5, -25 - i * 8);
      this.scene.add(group);
      this.airships.push({
        group,
        speed: 0.8 + i * 0.25,
        range: 52,
        phase: i * 2.2,
      });
    }
  }

  private buildDust(scene: Scene) {
    const positions = new Float32Array(600 * 3);
    for (let i = 0; i < 600; i += 1) {
      positions[i * 3] = randomRange(-25, 25);
      positions[i * 3 + 1] = randomRange(0.4, 10);
      positions[i * 3 + 2] = randomRange(-25, 25);
    }
    const geometry = new BufferGeometry();
    geometry.setAttribute('position', new Float32BufferAttribute(positions, 3));
    const material = new PointsMaterial({
      size: 0.065,
      map: this.noiseTexture,
      color: new Color(0.56, 0.76, 1),
      transparent: true,
      opacity: 0.36,
      blending: AdditiveBlending,
      depthWrite: false,
      sizeAttenuation: true,
    });
    const dust = new Points(geometry, material);
    scene.add(dust);
    this.tickers.push((time) => {
      dust.rotation.y = time * 0.005;
    });
  }

  private addContactShadow(parent: Group, width: number, depth: number, y: number) {
    const shadow = new Mesh(new PlaneGeometry(width, depth), this.contactShadowMaterial);
    shadow.rotation.x = -Math.PI / 2;
    shadow.position.y = y;
    shadow.renderOrder = 3;
    parent.add(shadow);
  }

  private addCollider(object: Object3D) {
    object.updateMatrixWorld(true);
    this.colliders.push(new Box3().setFromObject(object));
  }

  private addBoxCollider(
    x: number,
    y: number,
    z: number,
    width: number,
    height: number,
    depth: number,
  ) {
    this.colliders.push(
      new Box3(
        new Vector3(x - width / 2, y - height / 2, z - depth / 2),
        new Vector3(x + width / 2, y + height / 2, z + depth / 2),
      ),
    );
  }

  setMenuMode(enabled: boolean) {
    if (this.menuLight) {
      this.menuLight.visible = enabled;
      this.menuLight.intensity = 1050;
    }
    if (this.menuWarm) {
      this.menuWarm.visible = enabled;
    }
  }

  setQuality(quality: QualityLevel) {
    if (this.floorReflector) {
      this.floorReflector.visible = quality === 'cinematic';
    }
    const material = this.floorOverlay.material as MeshPhysicalMaterial;
    if (quality === 'cinematic') {
      material.alphaMap = this.puddleAlpha;
      material.transparent = true;
      material.depthWrite = false;
    } else {
      material.alphaMap = null;
      material.transparent = false;
      material.depthWrite = true;
    }
    material.needsUpdate = true;
    for (const light of this.optionalLights) {
      light.visible = quality === 'cinematic';
    }
    this.rain.setQuality(quality === 'cinematic');
    this.mist.group.forEach((sprite) => {
      const spriteMaterial = sprite.material as SpriteMaterial;
      spriteMaterial.opacity = quality === 'cinematic' ? 0.018 : 0.012;
    });
  }

  update(dt: number, cameraPosition: Vector3, time: number) {
    this.rain.update(dt, cameraPosition);
    this.ripples.update(time);
    this.mist.update(time);

    for (const ticker of this.tickers) {
      ticker(time, dt);
    }

    for (const puff of this.steam) {
      const progress = (time * puff.speed + puff.phase) % 1;
      puff.sprite.position.set(
        puff.base.x + Math.sin(progress * 5 + puff.phase * 8) * 0.34,
        puff.base.y + progress * 3.6,
        puff.base.z + Math.cos(progress * 4 + puff.phase * 5) * 0.34,
      );
      puff.sprite.scale.setScalar(puff.scale * (0.6 + progress * 1.5));
      const material = puff.sprite.material as SpriteMaterial;
      material.opacity = (1 - progress) * 0.15;
    }

    for (const { light, phase, base } of this.neonLights) {
      const flicker = Math.sin(time * 8.7 + phase) > 0.97 ? 0.25 : 1;
      light.intensity = base * (0.9 + Math.sin(time * 1.6 + phase) * 0.1) * flicker;
    }

    for (const airship of this.airships) {
      airship.group.position.x =
        Math.sin(time * 0.018 * airship.speed + airship.phase) * airship.range;
      airship.group.position.y =
        16 + Math.sin(time * 0.3 + airship.phase) * 0.35 + (airship.group.position.z < -30 ? 4 : 0);
    }

    this.reactorLight.intensity = 28 * (0.82 + Math.sin(time * 2.2) * 0.12);
  }

  dispose() {
    this.rain.dispose();
    this.ripples.dispose();
    this.mist.dispose();
    this.scene.traverse((object) => {
      if (!(object instanceof Mesh) && !(object instanceof Points) && !(object instanceof Sprite)) {
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
    if (this.floorReflector) {
      this.floorReflector.getRenderTarget().dispose();
    }
    this.glowTexture.dispose();
    this.smokeTexture.dispose();
    this.puddleAlpha.dispose();
    this.noiseTexture.dispose();
    this.contactShadow.dispose();
  }
}
