import type { CSSProperties } from 'react';
import type { GameSnapshot } from './engine/types';
import { formatTime } from './engine/utils';
import styles from './NeonVeil.module.css';

export interface KillFeedItem {
  id: number;
  label: string;
  points?: number;
  headshot?: boolean;
}

export interface HitStamp {
  id: number;
  headshot: boolean;
}

interface GameHudProps {
  snapshot: GameSnapshot;
  feed: KillFeedItem[];
  hitStamp: HitStamp;
  damageStamp: number;
  banner: { id: number; label: string; sub: string } | null;
  muted: boolean;
  onToggleMute: () => void;
}

const CARDINALS: Record<number, string> = {
  0: 'N',
  45: 'NE',
  90: 'E',
  135: 'SE',
  180: 'S',
  225: 'SW',
  270: 'W',
  315: 'NW',
};

function normalizeHeading(value: number) {
  return ((value % 360) + 360) % 360;
}

export function GameHud({
  snapshot,
  feed,
  hitStamp,
  damageStamp,
  banner,
  muted,
  onToggleMute,
}: GameHudProps) {
  const healthRatio = snapshot.health / snapshot.maxHealth;
  const shieldRatio = snapshot.shield / snapshot.maxShield;
  const lowHealth = snapshot.health <= 34;
  const compassTicks = Array.from({ length: 25 }, (_, index) => -60 + index * 5);
  const exactHeading = Math.round(normalizeHeading(snapshot.heading));
  const reloadPercent = Math.round(snapshot.reloadProgress * 100);
  const grenadePercent = snapshot.grenadeReady
    ? 100
    : Math.round((1 - snapshot.grenadeCooldown / 16) * 100);
  const crosshairStyle = {
    '--crosshair-gap': snapshot.aiming ? '4px' : snapshot.sprinting ? '18px' : '9px',
  } as CSSProperties;

  return (
    <div className={styles.hud}>
      <div className={styles.hudTop}>
        <div className={styles.brandBlock}>
          <div className={styles.brandRow}>
            <span className={styles.brandMark}>NV</span>
            <div>
              <p className={styles.brandName}>NEON VEIL</p>
              <p className={styles.brandSub}>KIRIN TACTICAL · LINK 07</p>
            </div>
          </div>
          <div className={styles.objective}>
            <span className={styles.objectiveLabel}>当前目标</span>
            <strong>{snapshot.objective}</strong>
          </div>
        </div>

        <div className={styles.compass}>
          <div className={styles.compassTrack}>
            {compassTicks.map((tick) => {
              const absolute = normalizeHeading(snapshot.heading + tick);
              const rounded = Math.round(absolute / 5) * 5;
              const cardinal = CARDINALS[rounded];
              const isMajor = rounded % 15 === 0;
              return (
                <span
                  key={tick}
                  className={`${styles.compassTick} ${cardinal ? styles.compassCardinal : ''}`}
                  style={{ left: `${50 + tick * 0.78}%` }}
                >
                  {cardinal ?? (isMajor ? String(rounded).padStart(3, '0') : '')}
                </span>
              );
            })}
          </div>
          <div className={styles.compassReadout}>
            <span>{String(exactHeading).padStart(3, '0')}°</span>
            <i />
          </div>
          <div className={styles.waveChip}>
            <span>WAVE {String(snapshot.wave).padStart(2, '0')}</span>
            <b>/</b>
            <span>{String(snapshot.totalWaves).padStart(2, '0')}</span>
            {snapshot.waveState === 'incoming' && (
              <em>{Math.max(1, Math.ceil(snapshot.waveCountdown))}s</em>
            )}
          </div>
        </div>

        <div className={styles.scoreBlock}>
          <div className={styles.scoreMain}>
            <span>SCORE</span>
            <strong>{snapshot.score.toLocaleString('en-US')}</strong>
          </div>
          <div className={styles.scoreGrid}>
            <span>
              击杀 <b>{snapshot.kills}</b>
            </span>
            <span>
              爆头 <b>{snapshot.headshots}</b>
            </span>
            <span>
              命中 <b>{snapshot.accuracy}%</b>
            </span>
            <span>
              用时 <b>{formatTime(snapshot.elapsed)}</b>
            </span>
          </div>
          <button type="button" className={styles.soundToggle} onClick={onToggleMute}>
            {muted ? 'SOUND OFF' : 'SOUND ON'}
          </button>
        </div>
      </div>

      {banner && (
        <div key={banner.id} className={styles.waveBanner}>
          <span className={styles.waveBannerLine} />
          <p>{banner.label}</p>
          <small>{banner.sub}</small>
        </div>
      )}

      <div className={styles.crosshair} style={crosshairStyle}>
        <i className={styles.crosshairTop} />
        <i className={styles.crosshairRight} />
        <i className={styles.crosshairBottom} />
        <i className={styles.crosshairLeft} />
        <span className={styles.crosshairDot} />
      </div>

      {hitStamp.id > 0 && (
        <div
          key={hitStamp.id}
          className={`${styles.hitMarker} ${hitStamp.headshot ? styles.hitMarkerHeadshot : ''}`}
        >
          <i />
          <i />
          <i />
          <i />
        </div>
      )}

      {damageStamp > 0 && <div key={`damage-${damageStamp}`} className={styles.damageFlash} />}
      {lowHealth && <div className={styles.lowHealthPulse} />}

      <div className={styles.killFeed}>
        {feed.map((item) => (
          <div
            key={item.id}
            className={`${styles.killFeedItem} ${item.headshot ? styles.killFeedHeadshot : ''}`}
          >
            <span>{item.headshot ? '◇' : '◆'}</span>
            <p>{item.label}</p>
            {item.points !== undefined && <b>+{item.points}</b>}
          </div>
        ))}
      </div>

      <div className={styles.hudBottom}>
        <div className={`${styles.vitals} ${lowHealth ? styles.vitalsLow : ''}`}>
          <div className={styles.vitalHeader}>
            <span>OPERATOR · K-17</span>
            <b>{snapshot.health}</b>
          </div>
          <div className={styles.barRow}>
            <span>HP</span>
            <div className={styles.barTrack}>
              <div
                className={`${styles.barFill} ${styles.healthFill}`}
                style={{ width: `${Math.max(0, healthRatio * 100)}%` }}
              />
            </div>
            <em>{snapshot.health}</em>
          </div>
          <div className={styles.barRow}>
            <span>SH</span>
            <div className={styles.barTrack}>
              <div
                className={`${styles.barFill} ${styles.shieldFill}`}
                style={{ width: `${Math.max(0, shieldRatio * 100)}%` }}
              />
            </div>
            <em>{snapshot.shield}</em>
          </div>
          <div className={styles.statusChips}>
            <span className={snapshot.sprinting ? styles.chipActive : ''}>疾跑</span>
            <span className={snapshot.aiming ? styles.chipActive : ''}>瞄准</span>
            <span className={snapshot.crouching ? styles.chipActive : ''}>蹲伏</span>
          </div>
        </div>

        <div className={styles.weaponBlock}>
          <div className={styles.weaponMeta}>
            <span>VK-7 · 脉冲步枪</span>
            <b>{snapshot.reloading ? 'RELOADING' : 'AUTO'}</b>
          </div>
          <div className={styles.ammoRow}>
            <strong className={snapshot.ammo <= 6 ? styles.ammoLow : ''}>
              {String(snapshot.ammo).padStart(2, '0')}
            </strong>
            <span className={styles.ammoSeparator}>/</span>
            <em>{String(snapshot.reserve).padStart(3, '0')}</em>
          </div>
          <div className={styles.ammoBars}>
            {Array.from({ length: snapshot.magazineSize }, (_, index) => (
              <i
                key={index}
                className={index < snapshot.ammo ? styles.ammoBar : styles.ammoBarEmpty}
              />
            ))}
          </div>
          {snapshot.reloading && (
            <div className={styles.reloadTrack}>
              <div style={{ width: `${reloadPercent}%` }} />
              <span>更换弹匣 {reloadPercent}%</span>
            </div>
          )}
          <p className={styles.hint}>{snapshot.hint}</p>
        </div>

        <div className={styles.abilityBlock}>
          <div
            className={`${styles.grenadeOrb} ${snapshot.grenadeReady ? styles.grenadeReady : styles.grenadeCooling}`}
            style={{
              background: `conic-gradient(var(--nv-cyan) ${grenadePercent}%, rgba(120, 160, 180, 0.12) 0)`,
            }}
          >
            <div>
              <span>Q</span>
              <b>{snapshot.grenadeReady ? 'READY' : Math.ceil(snapshot.grenadeCooldown)}</b>
            </div>
          </div>
          <p>脉冲手雷</p>
          <small>{snapshot.enemiesRemaining} 个威胁信号</small>
          <div className={styles.telemetry}>
            <span style={{ width: `${Math.min(100, snapshot.fps / 1.2)}%` }} />
          </div>
          <small>
            {snapshot.fps} FPS · {snapshot.quality === 'cinematic' ? 'CINEMATIC' : 'PERFORMANCE'}
          </small>
        </div>
      </div>

      <div className={styles.edgeCorners}>
        <i />
        <i />
        <i />
        <i />
      </div>
    </div>
  );
}
