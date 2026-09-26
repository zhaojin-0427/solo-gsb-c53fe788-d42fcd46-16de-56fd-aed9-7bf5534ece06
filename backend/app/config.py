"""全局常量：算法版本、判定阈值、置信度分级。

所有阈值集中在此，导出 JSON 会携带 PARAMS，保证判定依据可复现。
"""

ALGORITHM_VERSION = "1.0.0"

# ---- 音频分析 ----
SAMPLE_RATE = 22050
ONSET_MIN_GAP_S = 0.06          # 小于该间隔的起音合并
SEG_MIN_DURATION_S = 0.05       # 过短片段仍保留但标 low_voicing
VOICED_RATIO_SILENT = 0.25      # 段内有声帧比例低于此值 -> 无声歧义
POLY_RATIO_AMBIG = 0.25         # 双峰帧比例高于此值 -> 多声部歧义
F0_TOP_DOMINANCE = 0.55         # 主峰能量占比低于此值 -> 音高不稳

# ---- DP 对齐代价 ----
# 设计：同音 ~0；邻半音内偏音仍以"错音"配对（pitch_cost < delete+insert）；
# 相差较远（真正的额外音）则 insert+delete 更便宜，从而不会被强配。
COST_PITCH_PER_SEMI = 0.6       # 每个半音偏差
COST_PITCH_MAX = 4.0            # 音高代价上限
PITCH_MATCH_MAX_SEMI = 6.0      # 偏差超过 6 半音禁止自动配对 -> 走漏音+多音
COST_DELETE = 3.0               # 漏音（乐谱音未出现）
COST_INSERT = 3.0               # 多音（额外音）
COST_INSERT_PENDING = 0.8       # 歧义/无声观测作为"暂不强配"的保留代价
COST_ONSET_PER_SEC = 1.2        # 起音偏离期望时间的代价（每秒）
COST_ONSET_MAX = 1.2            # 起音代价上限，避免局部速度变化被重罚
COST_LOCKED_PENALTY = 0.0       # 锁定锚点强制对齐

# ---- 判定阈值 ----
PITCH_WRONG_SEMITONES = 1.0     # |Δ半音| >= 1 判错音
ONSET_OFFSET_S = 0.12           # |起音偏差| >= 0.12s 标起音偏移
DURATION_RATIO_LOW = 0.80       # 实际/期望 < 0.80
DURATION_RATIO_HIGH = 1.20      # 实际/期望 > 1.20
DURATION_MIN_DEV_S = 0.08       # 时值绝对偏差低于此值不报

# ---- 置信度（0~1） ----
CONF_HIGH = 0.80
CONF_MED = 0.50
# 多次演奏对比：任一侧置信度低于该阈值的音符判为"不可比"，
# 复用现有中档置信度阈值，不把低置信结果算作改善/回退。
COMPARE_MIN_CONFIDENCE = CONF_MED
COMPARE_MIN_MEMBERS = 2
COMPARE_MAX_MEMBERS = 6
CONF_MARGIN_SCALE = 3.4         # DP 次优间隔 -> 置信度
CONF_MATCH_BASE = 0.55
CONF_LOCKED = 0.99
CONF_PENDING = 0.30
CONF_DELETE_BASE = 0.7
CONF_INSERT_STABLE_BASE = 0.7

# ---- 最佳片段合成轨 ----
# 选源全局代价 = 异常数 * W_ANOMALY
#              + 切换次数 * W_SWITCH
#              + Σ 切点两侧响度差(dB) * W_LOUDNESS_PER_DB
#              + Σ 切点两侧局部速度比之差 * W_TEMPO_RATIO
# 权重全部公开（见合成清单 params 段与界面说明），不使用任何隐式权重。
SYNTH_ALGORITHM_VERSION = "1.0.0"
SYNTH_MIN_CONFIDENCE = CONF_MED     # 只允许"已确认且置信度达标"的片段
SYNTH_W_ANOMALY = 10.0              # 单个异常谱音（错音/起音偏移/时值偏差）
SYNTH_W_SWITCH = 3.0                # 单次来源切换
SYNTH_W_LOUDNESS_PER_DB = 1.0       # 切点两侧 RMS 响度差，每 dB
SYNTH_W_TEMPO_RATIO = 2.0           # 切点两侧局部速度比之差（|r左-r右|）
SYNTH_CROSSFADE_S = 0.02            # 切点短交叉淡化（单侧 20ms）
SYNTH_LOUDNESS_WINDOW_S = 0.06      # 起音后 RMS 响度窗口
SYNTH_DB_FLOOR = -80.0              # 静音窗口的响度下限
SYNTH_OUTPUT_HEADROOM = 0.99        # 输出 PCM 峰值上限（确定性归一化）

# ---- 偏差练习处方与复测 ----
RX_ALGORITHM_VERSION = "1.0.0"
RX_MAX_SEGMENTS = 20                # 每张处方最多框选的连续谱段数
RX_MIN_CONFIDENCE = CONF_MED        # 低于该置信度的谱音只能判“不可判定”
RX_CATEGORIES = (
    "wrong_pitch", "missing", "extra",
    "onset_offset", "duration_deviation",
)
# 未显式给出阈值时采用的默认最大误差（与全局判定阈值一致，可逐段收紧）
RX_DEFAULT_MAX_ONSET_S = ONSET_OFFSET_S
RX_DEFAULT_DURATION_RATIO_DEV = round(1.0 - DURATION_RATIO_LOW, 4)  # 0.20

# ---- 偏差片段练习谱 ----
# 在单次复盘中按展开后的整小节（measure + occurrence 反复轮次）框选连续片段，
# 逐段设置循环次数 / 起始速度(BPM) / 逐轮增速(BPM)，从原 MusicXML 提取对应
# 演奏轮次的整小节生成练习谱；创建时固化来源谱哈希 / 对齐版本 / 生成参数，
# 来源变化只标旧稿过期，显式重建才生成新稿。
PRACTICE_ALGORITHM_VERSION = "1.0.0"
PRACTICE_MAX_SEGMENTS = 20            # 一张练习谱最多片段数
PRACTICE_MIN_CONFIDENCE = CONF_MED    # 低于该置信度的谱位阻止生成
PRACTICE_MAX_LOOPS = 99               # 单段循环次数上限
PRACTICE_MIN_TEMPO_BPM = 20.0
PRACTICE_MAX_TEMPO_BPM = 300.0
PRACTICE_MAX_TEMPO_STEP_BPM = 100.0
# 练习谱每轮速度方向标记
PRACTICE_TEMPO_DIRECTIONS = ("up", "same", "down")

# ---- 谱面修订迁移 ----
# 选择一条已分析录音及其当前 MusicXML，上传修订版**单声部** MusicXML；
# 两侧均展开反复后，按（原谱小节、反复轮次、拍位、音高、时值）做确定性匹配，
# 把每个谱位归入 保留/移动/修改/删除/新增/无法匹配；再逐项标注历史锚点、
# 对齐快照、未过期对比、处方、练习谱为 可继承/需复核/失效。
# 匹配冲突（谱位键重复 / 移动配对非唯一 / 缺原谱坐标）与多声部歧义一律
# 阻止提交。用户确认后才创建新的谱面修订记录与供后续录音使用的复盘入口，
# 旧记录只读、不改写。
REVISION_ALGORITHM_VERSION = "1.0.0"
# 视为“移动”的最大移动代价（代价越大越倾向删除+新增而非移动）
REVISION_COST_DELETE = 1.0       # 旧谱位删除代价
REVISION_COST_INSERT = 1.0       # 新谱位新增代价
REVISION_COST_MODIFIED = 0.4     # 同谱位（小节/轮次/拍位一致）音高或时值变化
REVISION_COST_UNIT_PER_GAP = 0.3   # 移动：每跨一个展开小节单位
REVISION_COST_BEAT_PER_Q = 0.02    # 移动：每四分音符展开拍位差
# 时值完全一致判定（四分音符，浮点容差）
REVISION_DUR_EPS = 1e-3

# 浮点导出保留位数
ROUND_NDIGITS = 6
