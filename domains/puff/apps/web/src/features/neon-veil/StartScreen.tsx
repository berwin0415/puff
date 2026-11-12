import type { QualityLevel } from './engine/types';
import styles from './NeonVeil.module.css';

interface StartScreenProps {
  quality: QualityLevel;
  muted: boolean;
  onStart: () => void;
  onQualityChange: (quality: QualityLevel) => void;
  onToggleMute: () => void;
}

const CONTROLS = [
  ['WASD', '移动'],
  ['SHIFT', '疾跑'],
  ['SPACE', '跳跃'],
  ['CTRL / C', '蹲伏'],
  ['LMB', '射击'],
  ['RMB', '瞄准'],
  ['R', '换弹'],
  ['Q', '脉冲手雷'],
];

export function StartScreen({
  quality,
  muted,
  onStart,
  onQualityChange,
  onToggleMute,
}: StartScreenProps) {
  return (
    <section className={styles.startScreen}>
      <div className={styles.startNoise} />
      <div className={styles.startTop}>
        <span>KIRIN HEAVY INDUSTRIES</span>
        <span>SECTOR 07 · ROOFTOP REACTOR</span>
        <span className={styles.online}>
          <i /> LINK ONLINE
        </span>
      </div>

      <div className={styles.startBody}>
        <div className={styles.startKicker}>
          <span>OPERATION</span>
          <b>NIGHTFALL</b>
        </div>

        <div className={styles.titleWrap}>
          <span className={styles.titleGhost} aria-hidden="true">
            07
          </span>
          <h1 className={styles.gameTitle}>
            <span>NEON</span>
            <span>VEIL</span>
          </h1>
          <p className={styles.gameTitleCn}>霓 虹 帷 幕</p>
          <div className={styles.titleRule}>
            <i />
            <span>TACTICAL FPS / VERTICAL SLICE</span>
            <i />
          </div>
        </div>

        <div className={styles.startBriefing}>
          <p>
            <b>雨夜 23:47</b> · 第七区反应堆失去联络。失控的「麒麟」安保机械正在突破内环防线。
          </p>
          <p>接管 K-17 作战单元，守住反应堆，直到黎明前的最后一波信号熄灭。</p>
        </div>

        <div className={styles.startActions}>
          <button type="button" className={styles.deployButton} onClick={onStart}>
            <span>进入行动</span>
            <small>DEPLOY</small>
          </button>

          <div className={styles.qualityPicker}>
            <span>视觉预设</span>
            <div>
              <button
                type="button"
                className={quality === 'cinematic' ? styles.qualityActive : ''}
                onClick={() => onQualityChange('cinematic')}
              >
                电影级
              </button>
              <button
                type="button"
                className={quality === 'performance' ? styles.qualityActive : ''}
                onClick={() => onQualityChange('performance')}
              >
                性能
              </button>
            </div>
          </div>

          <button type="button" className={styles.muteButton} onClick={onToggleMute}>
            {muted ? '开启声音' : '关闭声音'}
          </button>
        </div>

        <div className={styles.controlGrid}>
          {CONTROLS.map(([key, label]) => (
            <div key={key}>
              <kbd>{key}</kbd>
              <span>{label}</span>
            </div>
          ))}
        </div>
      </div>

      <aside className={styles.menuTelemetry}>
        <span>LIVE FEED · REACTOR CAM</span>
        <b>
          SECTOR
          <br />
          07
        </b>
        <div className={styles.signalBars}>
          <i />
          <i />
          <i />
          <i />
          <i />
        </div>
        <dl>
          <dt>CORE TEMP</dt>
          <dd>87.4 °C</dd>
          <dt>HOSTILES</dt>
          <dd>04 ACTIVE</dd>
          <dt>WEATHER</dt>
          <dd>HEAVY RAIN</dd>
          <dt>LINK</dt>
          <dd>ENCRYPTED</dd>
        </dl>
      </aside>

      <div className={styles.startFooter}>
        <span>建议佩戴耳机 · 推荐 1440p 以上分辨率</span>
        <span className={styles.footerSerial}>NV-07 / BUILD 1.0.7</span>
      </div>

      <div className={styles.startVignette} />
      <div className={styles.startScanlines} />
    </section>
  );
}
