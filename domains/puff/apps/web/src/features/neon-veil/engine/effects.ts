import {
  AdditiveBlending,
  BufferGeometry,
  Color,
  DoubleSide,
  Float32BufferAttribute,
  Line,
  LineBasicMaterial,
  Mesh,
  MeshBasicMaterial,
  PlaneGeometry,
  PointLight,
  Points,
  PointsMaterial,
  RingGeometry,
  type Scene,
  Sprite,
  SpriteMaterial,
  type Texture,
  Vector3,
} from 'three';
import { randomRange } from './utils';

interface Burst {
  points: Points;
  positions: Float32Array;
  velocities: Float32Array;
  material: PointsMaterial;
  life: number;
  maxLife: number;
  gravity: number;
}

interface Tracer {
  line: Line;
  material: LineBasicMaterial;
  life: number;
}

interface Decal {
  mesh: Mesh;
  material: MeshBasicMaterial;
  life: number;
}

interface Smoke {
  sprite: Sprite;
  velocity: Vector3;
  life: number;
  maxLife: number;
  growth: number;
}

interface Wave {
  mesh: Mesh;
  material: MeshBasicMaterial;
  life: number;
  maxLife: number;
  speed: number;
}

interface FlashLight {
  light: PointLight;
  life: number;
  maxLife: number;
  intensity: number;
}

export class Effects {
  private readonly scene: Scene;
  private readonly glow: Texture;
  private readonly scorch: Texture;
  private readonly smoke: Texture;
  private readonly bursts: Burst[] = [];
  private readonly tracers: Tracer[] = [];
  private readonly decals: Decal[] = [];
  private readonly smokes: Smoke[] = [];
  private readonly waves: Wave[] = [];
  private readonly lights: FlashLight[] = [];

  constructor(scene: Scene, glow: Texture, scorch: Texture, smoke: Texture) {
    this.scene = scene;
    this.glow = glow;
    this.scorch = scorch;
    this.smoke = smoke;
  }

  spawnImpact(point: Vector3, normal: Vector3, color = 0xffb35e, count = 18) {
    const positions = new Float32Array(count * 3);
    const velocities = new Float32Array(count * 3);
    for (let i = 0; i < count; i += 1) {
      positions[i * 3] = point.x;
      positions[i * 3 + 1] = point.y;
      positions[i * 3 + 2] = point.z;
      const direction = normal
        .clone()
        .add(new Vector3(randomRange(-0.8, 0.8), randomRange(-0.2, 1), randomRange(-0.8, 0.8)))
        .normalize()
        .multiplyScalar(randomRange(1.5, 6));
      velocities[i * 3] = direction.x;
      velocities[i * 3 + 1] = direction.y;
      velocities[i * 3 + 2] = direction.z;
    }
    this.addBurst(positions, velocities, color, 0.45, 9, randomRange(0.08, 0.16));

    const decalMaterial = new MeshBasicMaterial({
      map: this.scorch,
      transparent: true,
      opacity: 0.9,
      depthWrite: false,
      polygonOffset: true,
      polygonOffsetFactor: -4,
      toneMapped: false,
    });
    const decalMesh = new Mesh(new PlaneGeometry(0.34, 0.34), decalMaterial);
    decalMesh.position.copy(point).addScaledVector(normal, 0.012);
    decalMesh.lookAt(point.clone().add(normal));
    decalMesh.renderOrder = 4;
    this.scene.add(decalMesh);
    this.decals.push({ mesh: decalMesh, material: decalMaterial, life: 22 });
    if (this.decals.length > 42) {
      const oldest = this.decals.shift();
      if (oldest) {
        this.scene.remove(oldest.mesh);
        oldest.mesh.geometry.dispose();
        oldest.material.dispose();
      }
    }
  }

  spawnTracer(from: Vector3, to: Vector3, color = 0xffd7a1) {
    const geometry = new BufferGeometry();
    geometry.setAttribute(
      'position',
      new Float32BufferAttribute([from.x, from.y, from.z, to.x, to.y, to.z], 3),
    );
    const material = new LineBasicMaterial({
      color: new Color(color).multiplyScalar(2.2),
      transparent: true,
      opacity: 0.9,
      blending: AdditiveBlending,
      depthWrite: false,
      toneMapped: false,
    });
    const line = new Line(geometry, material);
    line.renderOrder = 12;
    this.scene.add(line);
    this.tracers.push({ line, material, life: 0.075 });
  }

  spawnExplosion(position: Vector3, color = 0x6ff5ff) {
    this.spawnImpact(position, new Vector3(0, 1, 0), color, 70);
    for (let i = 0; i < 4; i += 1) {
      const material = new SpriteMaterial({
        map: this.smoke,
        color: new Color(0.5, 0.55, 0.6),
        transparent: true,
        opacity: 0.38,
        depthWrite: false,
      });
      const sprite = new Sprite(material);
      sprite.position.copy(position);
      sprite.position.x += randomRange(-0.5, 0.5);
      sprite.position.y += randomRange(-0.2, 0.7);
      sprite.position.z += randomRange(-0.5, 0.5);
      sprite.scale.setScalar(randomRange(1.2, 3));
      this.scene.add(sprite);
      this.smokes.push({
        sprite,
        velocity: new Vector3(
          randomRange(-0.3, 0.3),
          randomRange(0.8, 1.7),
          randomRange(-0.3, 0.3),
        ),
        life: randomRange(1.4, 2.4),
        maxLife: 2.4,
        growth: randomRange(1.5, 2.4),
      });
    }

    const waveMaterial = new MeshBasicMaterial({
      color: new Color(color).multiplyScalar(2.4),
      transparent: true,
      opacity: 0.85,
      blending: AdditiveBlending,
      depthWrite: false,
      side: DoubleSide,
      toneMapped: false,
    });
    const ring = new Mesh(new RingGeometry(0.4, 0.52, 64), waveMaterial);
    ring.rotation.x = -Math.PI / 2;
    ring.position.copy(position);
    ring.position.y = Math.max(0.08, position.y - 0.2);
    ring.renderOrder = 15;
    this.scene.add(ring);
    this.waves.push({ mesh: ring, material: waveMaterial, life: 0.55, maxLife: 0.55, speed: 12 });

    const light = new PointLight(color, 300, 18, 2);
    light.position.copy(position);
    light.position.y += 0.8;
    this.scene.add(light);
    this.lights.push({ light, life: 0.5, maxLife: 0.5, intensity: 300 });
  }

  spawnEnemyDeath(position: Vector3, color = 0xff7a3b) {
    this.spawnImpact(position, new Vector3(0, 1, 0), color, 40);
    const material = new SpriteMaterial({
      map: this.smoke,
      color: new Color(0.2, 0.16, 0.14),
      transparent: true,
      opacity: 0.45,
      depthWrite: false,
    });
    const sprite = new Sprite(material);
    sprite.position.copy(position);
    sprite.position.y += 0.8;
    sprite.scale.setScalar(2.4);
    this.scene.add(sprite);
    this.smokes.push({
      sprite,
      velocity: new Vector3(0, 0.7, 0),
      life: 1.6,
      maxLife: 1.6,
      growth: 2.2,
    });
  }

  spawnSpawnPillar(position: Vector3, color = 0x61f5ff) {
    const material = new MeshBasicMaterial({
      color: new Color(color).multiplyScalar(2),
      transparent: true,
      opacity: 0.7,
      blending: AdditiveBlending,
      depthWrite: false,
      side: DoubleSide,
      toneMapped: false,
    });
    const pillar = new Mesh(new RingGeometry(0.25, 0.4, 32), material);
    pillar.rotation.x = -Math.PI / 2;
    pillar.position.copy(position);
    pillar.position.y = 0.06;
    this.scene.add(pillar);
    this.waves.push({
      mesh: pillar,
      material,
      life: 0.9,
      maxLife: 0.9,
      speed: 4.5,
    });
    this.spawnImpact(position.clone().setY(0.8), new Vector3(0, 1, 0), color, 30);
  }

  private addBurst(
    positions: Float32Array,
    velocities: Float32Array,
    color: number,
    life: number,
    gravity: number,
    size: number,
  ) {
    const geometry = new BufferGeometry();
    geometry.setAttribute('position', new Float32BufferAttribute(positions, 3));
    const material = new PointsMaterial({
      map: this.glow,
      color: new Color(color).multiplyScalar(2.1),
      size,
      transparent: true,
      opacity: 1,
      blending: AdditiveBlending,
      depthWrite: false,
      sizeAttenuation: true,
      toneMapped: false,
    });
    const points = new Points(geometry, material);
    points.frustumCulled = false;
    points.renderOrder = 14;
    this.scene.add(points);
    this.bursts.push({
      points,
      positions,
      velocities,
      material,
      life,
      maxLife: life,
      gravity,
    });
  }

  update(dt: number) {
    for (let i = this.bursts.length - 1; i >= 0; i -= 1) {
      const burst = this.bursts[i];
      if (!burst) {
        continue;
      }
      burst.life -= dt;
      if (burst.life <= 0) {
        this.scene.remove(burst.points);
        burst.points.geometry.dispose();
        burst.material.dispose();
        this.bursts.splice(i, 1);
        continue;
      }
      for (let j = 0; j < burst.positions.length / 3; j += 1) {
        burst.velocities[j * 3 + 1] -= burst.gravity * dt;
        burst.positions[j * 3] += (burst.velocities[j * 3] ?? 0) * dt;
        burst.positions[j * 3 + 1] += (burst.velocities[j * 3 + 1] ?? 0) * dt;
        burst.positions[j * 3 + 2] += (burst.velocities[j * 3 + 2] ?? 0) * dt;
        if (burst.positions[j * 3 + 1] < 0.04) {
          burst.positions[j * 3 + 1] = 0.04;
          burst.velocities[j * 3] = (burst.velocities[j * 3] ?? 0) * 0.6;
          burst.velocities[j * 3 + 1] = Math.abs(burst.velocities[j * 3 + 1] ?? 0) * 0.35;
          burst.velocities[j * 3 + 2] = (burst.velocities[j * 3 + 2] ?? 0) * 0.6;
        }
      }
      const attribute = burst.points.geometry.getAttribute('position');
      attribute.needsUpdate = true;
      burst.material.opacity = Math.min(1, burst.life / (burst.maxLife * 0.5));
    }

    for (let i = this.tracers.length - 1; i >= 0; i -= 1) {
      const tracer = this.tracers[i];
      if (!tracer) {
        continue;
      }
      tracer.life -= dt;
      tracer.material.opacity = Math.max(0, tracer.life / 0.075) * 0.9;
      if (tracer.life <= 0) {
        this.scene.remove(tracer.line);
        tracer.line.geometry.dispose();
        tracer.material.dispose();
        this.tracers.splice(i, 1);
      }
    }

    for (let i = this.decals.length - 1; i >= 0; i -= 1) {
      const decal = this.decals[i];
      if (!decal) {
        continue;
      }
      decal.life -= dt;
      if (decal.life < 3) {
        decal.material.opacity = Math.max(0, decal.life / 3) * 0.9;
      }
      if (decal.life <= 0) {
        this.scene.remove(decal.mesh);
        decal.mesh.geometry.dispose();
        decal.material.dispose();
        this.decals.splice(i, 1);
      }
    }

    for (let i = this.smokes.length - 1; i >= 0; i -= 1) {
      const smoke = this.smokes[i];
      if (!smoke) {
        continue;
      }
      smoke.life -= dt;
      smoke.sprite.position.addScaledVector(smoke.velocity, dt);
      smoke.sprite.scale.addScalar(smoke.growth * dt);
      const material = smoke.sprite.material as SpriteMaterial;
      material.opacity = Math.max(0, smoke.life / smoke.maxLife) * 0.35;
      if (smoke.life <= 0) {
        this.scene.remove(smoke.sprite);
        material.dispose();
        this.smokes.splice(i, 1);
      }
    }

    for (let i = this.waves.length - 1; i >= 0; i -= 1) {
      const wave = this.waves[i];
      if (!wave) {
        continue;
      }
      wave.life -= dt;
      const progress = 1 - wave.life / wave.maxLife;
      wave.mesh.scale.setScalar(1 + progress * wave.speed);
      wave.material.opacity = Math.max(0, 1 - progress) * 0.85;
      if (wave.life <= 0) {
        this.scene.remove(wave.mesh);
        wave.mesh.geometry.dispose();
        wave.material.dispose();
        this.waves.splice(i, 1);
      }
    }

    for (let i = this.lights.length - 1; i >= 0; i -= 1) {
      const flash = this.lights[i];
      if (!flash) {
        continue;
      }
      flash.life -= dt;
      flash.light.intensity = Math.max(0, flash.life / flash.maxLife) * flash.intensity;
      if (flash.life <= 0) {
        this.scene.remove(flash.light);
        this.lights.splice(i, 1);
      }
    }
  }

  reset() {
    for (const burst of this.bursts) {
      this.scene.remove(burst.points);
      burst.points.geometry.dispose();
      burst.material.dispose();
    }
    this.bursts.length = 0;
    for (const tracer of this.tracers) {
      this.scene.remove(tracer.line);
      tracer.line.geometry.dispose();
      tracer.material.dispose();
    }
    this.tracers.length = 0;
    for (const decal of this.decals) {
      this.scene.remove(decal.mesh);
      decal.mesh.geometry.dispose();
      decal.material.dispose();
    }
    this.decals.length = 0;
    for (const smoke of this.smokes) {
      this.scene.remove(smoke.sprite);
      (smoke.sprite.material as SpriteMaterial).dispose();
    }
    this.smokes.length = 0;
    for (const wave of this.waves) {
      this.scene.remove(wave.mesh);
      wave.mesh.geometry.dispose();
      wave.material.dispose();
    }
    this.waves.length = 0;
    for (const flash of this.lights) {
      this.scene.remove(flash.light);
    }
    this.lights.length = 0;
  }

  dispose() {
    for (const burst of this.bursts) {
      this.scene.remove(burst.points);
      burst.points.geometry.dispose();
      burst.material.dispose();
    }
    for (const tracer of this.tracers) {
      this.scene.remove(tracer.line);
      tracer.line.geometry.dispose();
      tracer.material.dispose();
    }
    for (const decal of this.decals) {
      this.scene.remove(decal.mesh);
      decal.mesh.geometry.dispose();
      decal.material.dispose();
    }
    for (const smoke of this.smokes) {
      this.scene.remove(smoke.sprite);
      (smoke.sprite.material as SpriteMaterial).dispose();
    }
    for (const wave of this.waves) {
      this.scene.remove(wave.mesh);
      wave.mesh.geometry.dispose();
      wave.material.dispose();
    }
    for (const flash of this.lights) {
      this.scene.remove(flash.light);
    }
  }
}
