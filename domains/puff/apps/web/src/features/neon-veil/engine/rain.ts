import {
  AdditiveBlending,
  BufferAttribute,
  BufferGeometry,
  Color,
  DynamicDrawUsage,
  Float32BufferAttribute,
  LineBasicMaterial,
  LineSegments,
  Points,
  ShaderMaterial,
  Sprite,
  SpriteMaterial,
  type Texture,
  Vector3,
} from 'three';
import { randomRange } from './utils';

interface Drop {
  x: number;
  y: number;
  z: number;
  speed: number;
  length: number;
}

export class RainSystem {
  readonly object: LineSegments;
  private readonly drops: Drop[] = [];
  private readonly positions: Float32Array;
  private readonly area: number;
  private readonly height: number;
  private wind = new Vector3(0.16, 0, 0.05);

  constructor(count = 5200, area = 54, height = 20) {
    this.area = area;
    this.height = height;
    this.positions = new Float32Array(count * 6);
    for (let i = 0; i < count; i += 1) {
      this.drops.push({
        x: randomRange(-area / 2, area / 2),
        y: randomRange(0, height),
        z: randomRange(-area / 2, area / 2),
        speed: randomRange(16, 28),
        length: randomRange(0.28, 0.9),
      });
    }

    const geometry = new BufferGeometry();
    const attribute = new BufferAttribute(this.positions, 3);
    attribute.setUsage(DynamicDrawUsage);
    geometry.setAttribute('position', attribute);
    const material = new LineBasicMaterial({
      color: new Color(0.56, 0.78, 1),
      transparent: true,
      opacity: 0.21,
      blending: AdditiveBlending,
      depthWrite: false,
    });
    this.object = new LineSegments(geometry, material);
    this.object.frustumCulled = false;
    this.object.renderOrder = 30;
    this.writePositions();
  }

  setQuality(cinematic: boolean) {
    const material = this.object.material as LineBasicMaterial;
    material.opacity = cinematic ? 0.23 : 0.15;
  }

  private writePositions() {
    for (let i = 0; i < this.drops.length; i += 1) {
      const drop = this.drops[i];
      if (!drop) {
        continue;
      }
      const offset = i * 6;
      this.positions[offset] = drop.x;
      this.positions[offset + 1] = drop.y;
      this.positions[offset + 2] = drop.z;
      this.positions[offset + 3] = drop.x + this.wind.x * 0.55;
      this.positions[offset + 4] = drop.y + drop.length;
      this.positions[offset + 5] = drop.z + this.wind.z * 0.55;
    }
    const attribute = this.object.geometry.getAttribute('position') as BufferAttribute;
    attribute.needsUpdate = true;
  }

  update(dt: number, position: Vector3) {
    const half = this.area / 2;
    for (const drop of this.drops) {
      drop.y -= drop.speed * dt;
      drop.x += this.wind.x * dt;
      drop.z += this.wind.z * dt;
      if (drop.y < 0.02) {
        drop.y = this.height;
        drop.x = position.x + randomRange(-half, half);
        drop.z = position.z + randomRange(-half, half);
      }
      if (drop.x < position.x - half) {
        drop.x += this.area;
      } else if (drop.x > position.x + half) {
        drop.x -= this.area;
      }
      if (drop.z < position.z - half) {
        drop.z += this.area;
      } else if (drop.z > position.z + half) {
        drop.z -= this.area;
      }
    }
    this.writePositions();
  }

  dispose() {
    this.object.geometry.dispose();
    (this.object.material as LineBasicMaterial).dispose();
  }
}

const rippleVertex = `
  attribute float aPhase;
  attribute float aScale;
  varying float vPhase;
  varying float vScale;
  void main() {
    vPhase = aPhase;
    vScale = aScale;
    vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * mvPosition;
    gl_PointSize = aScale * (20.0 / max(1.0, -mvPosition.z));
  }
`;

const rippleFragment = `
  uniform float uTime;
  varying float vPhase;
  varying float vScale;
  void main() {
    vec2 p = gl_PointCoord * 2.0 - 1.0;
    float distanceToCenter = length(p);
    float progress = fract(uTime * 0.55 + vPhase);
    float radius = progress * 0.92;
    float ring = smoothstep(0.09, 0.0, abs(distanceToCenter - radius));
    float fade = (1.0 - progress) * smoothstep(1.0, 0.55, distanceToCenter);
    float alpha = ring * fade * 0.2;
    if (alpha < 0.01) discard;
    gl_FragColor = vec4(0.55, 0.82, 1.0, alpha);
  }
`;

export class RippleField {
  readonly points: Points;
  private readonly material: ShaderMaterial;

  constructor(count = 240, area = 50) {
    const positions = new Float32Array(count * 3);
    const phases = new Float32Array(count);
    const scales = new Float32Array(count);
    for (let i = 0; i < count; i += 1) {
      positions[i * 3] = randomRange(-area / 2, area / 2);
      positions[i * 3 + 1] = 0.035;
      positions[i * 3 + 2] = randomRange(-area / 2, area / 2);
      phases[i] = Math.random();
      scales[i] = randomRange(5, 12);
    }
    const geometry = new BufferGeometry();
    geometry.setAttribute('position', new Float32BufferAttribute(positions, 3));
    geometry.setAttribute('aPhase', new Float32BufferAttribute(phases, 1));
    geometry.setAttribute('aScale', new Float32BufferAttribute(scales, 1));
    this.material = new ShaderMaterial({
      uniforms: { uTime: { value: 0 } },
      vertexShader: rippleVertex,
      fragmentShader: rippleFragment,
      transparent: true,
      depthWrite: false,
      blending: AdditiveBlending,
    });
    this.points = new Points(geometry, this.material);
    this.points.frustumCulled = false;
    this.points.renderOrder = 29;
  }

  update(time: number) {
    this.material.uniforms.uTime.value = time;
  }

  dispose() {
    this.points.geometry.dispose();
    this.material.dispose();
  }
}

interface MistPuff {
  sprite: Sprite;
  speed: number;
  phase: number;
  baseX: number;
  baseZ: number;
}

export class MistSystem {
  readonly group: Sprite[] = [];
  private readonly puffs: MistPuff[] = [];

  constructor(texture: Texture, count = 16, area = 42) {
    for (let i = 0; i < count; i += 1) {
      const material = new SpriteMaterial({
        map: texture,
        color: new Color(0.26, 0.34, 0.44),
        transparent: true,
        opacity: randomRange(0.035, 0.09),
        depthWrite: false,
        blending: AdditiveBlending,
      });
      const sprite = new Sprite(material);
      const x = randomRange(-area / 2, area / 2);
      const z = randomRange(-area / 2, area / 2);
      sprite.position.set(x, randomRange(0.15, 1.2), z);
      sprite.scale.setScalar(randomRange(4, 11));
      this.group.push(sprite);
      this.puffs.push({
        sprite,
        speed: randomRange(0.06, 0.18),
        phase: Math.random() * Math.PI * 2,
        baseX: x,
        baseZ: z,
      });
    }
  }

  update(time: number) {
    for (const puff of this.puffs) {
      puff.sprite.position.x = puff.baseX + Math.sin(time * 0.08 + puff.phase) * 2.2;
      puff.sprite.position.z = puff.baseZ + Math.cos(time * 0.06 + puff.phase) * 1.6;
      puff.sprite.position.y = 0.6 + Math.sin(time * puff.speed + puff.phase) * 0.25;
    }
  }

  dispose() {
    for (const sprite of this.group) {
      (sprite.material as SpriteMaterial).map?.dispose();
      (sprite.material as SpriteMaterial).dispose();
    }
  }
}
