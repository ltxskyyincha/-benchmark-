# 路径评分指标汇总

> 本文档汇总当前评价系统中全部 **22 个评分元函数**的评价理念、输入参数和计算方式。
>
> 每个元函数均为工厂函数：接收配置参数 → 返回闭包 `scorer(points) → (score, passed)`。
> 最终评价函数的唯一输入为路径经纬度列表 `list[tuple[float, float]]`。

---

## 总体架构

```
benchmark.json 配置
       │
       ▼
  build_evaluator(config)        ← factory.py
       │
       ├── 初等元函数 × 13       ← elementary_metrics.py
       ├── 中等元函数 × 9        ← intermediate_metrics.py
       │
       ▼
  evaluate(points) → {overall, hard_pass, metrics[]}
```

**硬约束机制**：任一 `hard=True` 的指标 `passed=False` → 综合分封顶至 `hard_fail_cap`（默认 0.5）；严格模式下直接置零。

**已移除的 2 个指标**（因 DB 信息损失）：`transport_mode`、`split_transport`。

---

## 一、初等元函数（13 个）

### 1. `max_total_distance` — 路径总长上限

**评价理念**：检查路径折线总长度是否不超过指定公里数上限。适用于「不超过 5 公里」类指令。

**参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `max_km` | float | 允许的最大总路程（公里） |

**计算逻辑**：

- 计算路径 Haversine 总长 `L`（公里）
- `L ≤ max_km` → score = 1.0, passed = True
- `L > max_km` → score = max(0, 1 − (L − max_km) / max_km), passed = False

---

### 2. `min_total_distance` — 路径总长下限

**评价理念**：检查路径总长度是否不低于指定公里数。适用于「不少于 5 公里」类指令。

**参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `min_km` | float | 要求的最小总路程（公里） |

**计算逻辑**：

- `L ≥ min_km` → score = 1.0, passed = True
- `L < min_km` → score = L / min_km, passed = False

---

### 3. `target_distance` — 目标里程

**评价理念**：检查路径总长与目标里程的偏差是否在容忍带内。适用于「约 5 公里 / 5 公里左右」类指令。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `target_km` | float | — | 目标路程（公里） |
| `tolerance_km` | float | 0.5 | 容忍偏差（公里） |

**计算逻辑**：

- δ = |L − target_km|
- δ ≤ tolerance → score = 1.0, passed = True
- δ > tolerance → score = max(0, 1 − (δ − tolerance) / tolerance), passed = False

---

### 4. `reference_distance` — 参考里程

**评价理念**：当指令未给出显式里程时（如「绕一圈」），用预先计算的参考里程（通常 = 区域周长 × 路网系数）评估。同时检查总长是否在合理区间内。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `target_km` | float | — | 参考目标路程 |
| `tolerance_km` | float | 1.0 | 容忍偏差 |
| `min_km` | float? | None | 合理区间下界 |
| `max_km` | float? | None | 合理区间上界 |

**计算逻辑**：

- **容忍带评分** band_s：同 target_distance 逻辑
- **合理区间评分** range_s：L 在 [min_km, max_km] 内 → 1.0；低于下界按比例扣分，高于上界线性衰减
- score = band_s × range_s
- passed = 容忍带内 **且** 合理区间内

---

### 5. `waypoint_coverage` — 必经点覆盖

**评价理念**：检查路径是否经过所有指定途经点（在各自半径内），以及是否按指定顺序依次经过。这是最常见的硬约束。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `waypoints` | list[dict] | — | 途经点列表，每个含 `lat`, `lon`, 可选 `radius_m`, `id`, `name` |
| `ordered` | bool | True | 是否要求按列表顺序经过 |
| `default_radius_m` | float | 80.0 | 默认到达判定半径（米） |

**计算逻辑**：

1. 对每个途经点，找路径上距其最近的点，判断距离 ≤ radius_m → 命中
2. 若 `ordered=True`，检查各命中点的弧长进度是否单调递增（最后一个途经点取最晚命中，其余取最早命中）
3. 全部命中且顺序正确 → score = 1.0, passed = True
4. 否则 → score = 命中率 × (顺序正确 ? 1.0 : 0.5), passed = False

---

### 6. `loop_to_start` — 闭环返回

**评价理念**：检查路径终点是否回到起点附近，形成闭环。或者回到指定的终点 POI。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `radius_m` | float | 100.0 | 闭环判定半径（米） |
| `end_waypoint` | dict? | None | 终点 POI 坐标 `{lat, lon}`；不提供时以路径首点为参考 |

**计算逻辑**：

- d = 首尾两点距离（或路径末端到终点 POI 的最近距离）
- d ≤ radius_m → score = 1.0, passed = True
- d > radius_m → score = max(0, 1 − d / (2 × radius_m)), passed = False

---

### 7. `no_backtrack_on_return` — 回程不重走

**评价理念**：检查去程和回程的路段重叠率是否足够低。适用于「回程不要原路返回」类指令。与 `require_same_route_return` **互斥**。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_overlap_ratio` | float | 0.15 | 允许的最大重叠率 |
| `waypoint_coords` | list? | None | 途经点坐标，辅助拆分去回程 |
| `db` | MapDatabase? | None | 地图数据库（由 factory 自动注入） |

**计算逻辑**：

1. 启发式拆分路径为去程/回程（以倒数第二个途经点的弧长进度为分割，否则按弧长中点）
2. 构建边集（两种模式）：
   - **DB 模式**（有 `db`）：将各段吸附到最近路网边，用 `way_id` 集合
   - **网格模式**（无 `db`）：将点离散化为网格单元格（精度 4 位小数）
3. overlap = |去程集 ∩ 回程集| / min(|去程集|, |回程集|)
4. overlap ≤ max_overlap_ratio → passed = True

---

### 8. `require_same_route_return` — 要求原路返回

**评价理念**：检查去程和回程的路段重叠率是否足够高。适用于「原路返回」类指令。与 `no_backtrack_on_return` **互斥**。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `min_overlap_ratio` | float | 0.7 | 要求的最小重叠率 |
| `waypoint_coords` | list? | None | 途经点坐标 |
| `db` | MapDatabase? | None | 地图数据库 |

**计算逻辑**：

- 拆分与边集构建同上
- overlap ≥ min_overlap_ratio → score = 1.0, passed = True
- 否则 → score = overlap / min_overlap_ratio, passed = False

---

### 9. `turn_preference` — 转向偏好

**评价理念**：统计路径中的左转/右转次数，检查是否满足「少左转」「少右转」的偏好要求。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `minimize` | str | "left_turn" | 要最小化的类型：`left_turn` / `right_turn` / `both` |
| `angle_threshold_deg` | float | 25.0 | 判定为转弯的最小方位角变化（度） |
| `max_count` | int | 6 | 允许的最大转弯次数 |

**计算逻辑**：

1. 遍历相邻三点 (p[i−1], p[i], p[i+1])，计算方位角变化 diff = bearing₂ − bearing₁（归一化到 [−180, 180]）
2. diff ≥ threshold → 左转 +1；diff ≤ −threshold → 右转 +1
3. 根据 `minimize` 选择计数目标
4. count ≤ max_count → score = 1.0, passed = True
5. 否则 → score = max(0, 1 − (count − max_count) / max_count), passed = False

---

### 10. `avoid_roads` — 避开指定道路

**评价理念**：检查路径是否途经了应避开的道路。适用于「避开中关村东路」「不要走成府路」类指令。通过 DB 路网边名称匹配实现。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `road_names` | list[str] | — | 要避开的道路名称列表 |
| `max_violation_ratio` | float | 0.0 | 允许的最大违规比率（0 = 完全不允许） |
| `db` | MapDatabase? | None | 地图数据库（由 factory 自动注入） |

**计算逻辑**：

1. 通过 DB 将路径各段吸附到最近路网边，获取边的道路名
2. 逐段检查名称是否匹配避开列表（双向子串包含，如「中关村东路」匹配「中关村东路北段」）
3. violation_ratio = 匹配段总长 / 路径总长
4. violation_ratio ≤ max_violation_ratio → score = 1.0, passed = True
5. 否则 → score = max(0, 1 − violation_ratio / max_violation_ratio), passed = False

> **回退行为**：当 DB 不可用时，无法获取路名，返回 (0.5, False) 降级结果。

---

### 11. `start_point` — 起点约束

**评价理念**：检查路径的**第一个点**是否落在指定位置附近。适用于「从北大东门出发」「起点在地铁站」类指令。与 `waypoint_coverage` 的区别是：必经点覆盖检查路径**任意位置**是否路过某点，而起点约束严格检查路径首点。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `lat` | float | — | 起点纬度 |
| `lon` | float | — | 起点经度 |
| `name` | str? | None | 起点名称（仅文档用途） |
| `radius_m` | float | 100.0 | 判定半径（米） |

**计算逻辑**：

1. d = haversine(points[0], (lat, lon))
2. d ≤ radius_m → score = 1.0, passed = True
3. 否则 → score = max(0, 1 − d / (2 × radius)), passed = False

---

### 12. `end_point` — 终点约束

**评价理念**：检查路径的**最后一个点**是否落在指定位置附近。适用于「最终到达军事博物馆」「终点是未名湖」类指令。结构与 `start_point` 完全对称，只是检查 points[-1]。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `lat` | float | — | 终点纬度 |
| `lon` | float | — | 终点经度 |
| `name` | str? | None | 终点名称（仅文档用途） |
| `radius_m` | float | 100.0 | 判定半径（米） |

**计算逻辑**：

1. d = haversine(points[-1], (lat, lon))
2. d ≤ radius_m → score = 1.0, passed = True
3. 否则 → score = max(0, 1 − d / (2 × radius)), passed = False

---

### 13. `avoid_point` — 避开某点

**评价理念**：路径需与指定地点保持距离，不得靠近。适用于「避开工地」「别经过垃圾站」「远离嘈杂市场」类指令。与 `avoid_roads` 互补——前者避开**线状道路**（按路网边名匹配），本指标避开**点状地点**（按坐标距离）。判定采用"路径最近距离"语义：路径任何一处都不能进入目标点的安全半径，最严格、最直观。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `lat` | float | — | 要避开的点纬度 |
| `lon` | float | — | 要避开的点经度 |
| `radius_m` | float | 150.0 | 安全半径（米），路径须保持在此距离之外 |
| `name` | str? | None | 点名称（仅文档用途） |

**计算逻辑**：

1. min_dist = 路径上所有点到目标点的最近距离
2. min_dist ≥ radius_m → 完全合格，score = 1.0, passed = True
3. 侵入安全圈（min_dist < radius_m）→ score = min_dist / radius_m, passed = False
   - 擦边时（min_dist 接近 radius）扣分少
   - 正好经过目标点时（min_dist ≈ 0）score ≈ 0

> 空路径视为未靠近，返回 (1.0, True)。

---

## 二、中等元函数（9 个）

### 1. `region_penetration` — 边界穿越禁止

**评价理念**：给定一个封闭边界，检查路径是否违规穿越。支持**两种模式**：
- **`no_enter`（默认）**：外部不得进入边界内部。适用于「绕清华跑一圈」不穿越校园。
- **`no_exit`**：内部不得离开边界。适用于「在公园里跑步」不跑出园、「在校园内活动」不出校。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `polygon` | list | — | 边界多边形 `[[lat, lon], ...]` |
| `mode` | str | "no_enter" | `no_enter`（外不入内）或 `no_exit`（内不出外） |
| `core_polygon` | list? | None | 核心区域多边形（仅 no_enter 模式用于穿入检测） |
| `max_violation_ratio` | float | 0.02 | 允许的最大违规点占比（兼容旧参数名 `max_interior_ratio`） |
| `max_violation_run_m` | float | 100.0 | 允许的最长连续违规弧长，米（兼容旧参数名 `max_interior_run_m_val`） |

**计算逻辑**：

1. 根据 mode 确定违规侧：
   - `no_enter` → 落在边界**内部**的点为违规
   - `no_exit` → 落在边界**外部**的点为违规
2. violation_ratio = 违规点数 / 总点数
3. violation_run_m = 最长连续违规段弧长
4. 两项都须 ≤ 阈值 → passed
5. s_ratio = 1 − violation_ratio / max_violation_ratio
6. s_run = 1 − run_m / max_violation_run_m
7. score = min(s_ratio, s_run)

> **向后兼容**：旧 benchmark 使用的 `max_interior_ratio` / `max_interior_run_m_val` 参数名仍然有效，会自动映射到新参数。不指定 mode 时默认 `no_enter`，行为与旧版完全一致。
> **注意**：score < 1.0 时 passed 可能为 True（在阈值范围内但非完美），这是设计意图。

---

### 2. `region_orbit_uniformity` — 绕行均衡性

**评价理念**：评估「绕 X 一圈」路径是否从各个方向均匀地包围了目标区域。从六个子维度综合评估，确保路径不会只在某一侧绕行。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `polygon` | list | — | 区域多边形 |
| `max_boundary_cv` | float | 0.45 | 边界距离最大变异系数 |
| `max_ring_cv` | float | 0.30 | 环带距离最大变异系数 |
| `max_side_cv` | float | 0.35 | 四向平衡最大变异系数 |
| `min_exterior_ratio` | float | 0.85 | 最小外围占比 |
| `min_sector_coverage` | float | 0.625 | 最小方位扇区覆盖率 |
| `min_offset_m` | float | 80.0 | 外部点平均偏移合理下限（米） |
| `max_offset_m` | float | 700.0 | 外部点平均偏移合理上限（米） |
| `n_sectors` | int | 8 | 方位扇区数 |

**计算逻辑（加权六维）**：

| 子维度 | 权重 | 含义 |
|--------|------|------|
| 外围占比 | 0.20 | 轨迹点在区域外部的比例，应 ≥ min_exterior_ratio |
| 边界距离 CV | 0.25 | 全轨迹点到区域边界距离的变异系数（穿入点记 0），CV 低 = 距离均匀 |
| 环带 CV | 0.20 | 各点到区域质心距离的 CV，低 CV = 轨迹呈环形 |
| 四向平衡 | 0.20 | 北/南/东/西四个方向平均边界距离的 CV |
| 偏移带 | 0.10 | 外部点平均偏移是否在 [min_offset, max_offset] 合理区间 |
| 方位覆盖 | 0.05 | 各方位扇区是否都有路径点覆盖 |

score = 各项加权和；passed = 各项宽松系数范围内全部满足。

---

### 3. `orbit_boundary_proximity` — 贴边最近路线

**评价理念**：检查绕行路径在各方位扇区上是否接近该方向上的最短边界距离，即路径应「贴外围走」而非在某些方向大幅外绕。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `polygon` | list | — | 区域多边形 |
| `core_polygon` | list? | None | 核心多边形（判定内部点跳过） |
| `tolerance_m` | float | 80.0 | 容忍偏移量（超出才计 excess） |
| `max_excess_ratio` | float | 0.12 | 允许的最大 excess 比率 |
| `max_sector_mean_excess_m` | float | 100.0 | 各扇区平均 excess 上限 |
| `n_sectors` | int | 16 | 方位扇区数 |

**计算逻辑**：

1. 将外部点按方位角分配到 n_sectors 个扇区
2. 每扇区记录该方向上路径点到边界的最小偏移 `ref`
3. 每个点的 excess = max(0, 偏移 − ref − tolerance)
4. excess_ratio = Σ(excess × 段长) / Σ(偏移 × 段长)
5. excess_ratio ≤ max_excess_ratio **且** 各扇区平均 excess ≤ 限值 → passed

---

### 4. `orbit_parallel_corridors` — 冗余平行走廊

**评价理念**：检测绕行路径是否在同一侧出现了冗余的平行路段。例如绕清华时同一方向走了外围和内部两条平行道路，属于不合理规划。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `polygon` | list | — | 区域多边形 |
| `core_polygon` | list? | None | 核心多边形 |
| `angle_tol_deg` | float | 30.0 | 方向相似判定角度阈值 |
| `min_segment_m` | float | 100.0 | 路径段最小长度 |
| `max_parallel_ratio` | float | 0.15 | 允许的最大冗余平行比率 |
| `perimeter_tolerance_m` | float | 80.0 | 判定段在外围轨道上的距离容忍 |
| `min_offset_delta_m` | float | 60.0 | 两段偏移差最小值 |
| `min_lateral_sep_m` | float | 150.0 | 横向最小间距 |
| `n_sectors` | int | 16 | 扇区数 |

**计算逻辑**：

1. 将路径合并为 ≥ min_segment_m 的段，记录方位角和平均偏移
2. 对所有段对逐一检查五项条件：方向相似（角度差 ≤ 阈值）、同侧（区域不在两段之间）、物理分离（横向间距 ≥ 阈值）、偏移差足够大、非都在外围轨道上
3. 满足全部条件 → 非外围段标记为冗余
4. parallel_ratio = 冗余段总长 / 总长 ≤ max_parallel_ratio → passed

---

### 5. `corridor_follow_uniformity` — 走廊跟随均衡

**评价理念**：评估路径沿目标走廊（河流、主路等）的跟随质量是否均匀。适用于「沿河跑 / 沿北四环」类指令。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `corridor` | list | — | 走廊折线 `[[lat, lon], ...]` |
| `buffer_m` | float | 120.0 | 缓冲区半径 |
| `max_cv` | float | 0.60 | 允许的最大距离变异系数 |
| `max_mean_deviation_m` | float | 80.0 | 允许的最大平均偏离 |
| `min_in_buffer_ratio` | float | 0.70 | 要求的最小缓冲区内点占比 |

**计算逻辑（加权三维）**：

| 子维度 | 权重 | 含义 |
|--------|------|------|
| 距离 CV | 0.35 | 各点到走廊距离的变异系数，低 = 均匀跟随 |
| 平均偏离 | 0.35 | 到走廊的平均距离 ≤ max_mean_deviation_m |
| 缓冲区覆盖 | 0.30 | buffer_m 范围内的点占比 ≥ min_in_buffer_ratio |

---

### 6. `must_pass_corridor` — 必经走廊

**评价理念**：检查路径是否经过指定走廊，且有足够长度的连续段在走廊附近。适用于「去程走北四环辅路 / 沿南长河走 500 米」类指令。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `corridor` | list | — | 走廊折线 |
| `buffer_m` | float | 120.0 | 缓冲区半径 |
| `min_continuous_m` | float | 200.0 | 要求的最小连续走廊段长度 |

**计算逻辑**：

- 计算路径中「连续两端点都在走廊 buffer_m 内」的最长子路径长度 longest_run
- longest_run ≥ min_continuous_m → score = 1.0, passed = True
- 否则 → score = longest_run / min_continuous_m, passed = False

---

### 7. `multi_lap` — 多圈检测

**评价理念**：通过累计方位角变化估算路径绕指定区域或中心点的圈数。适用于「绕福海两圈 / 绕操场三圈」类指令。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `target_laps` | int | 1 | 目标圈数 |
| `lap_tolerance` | float | 0.35 | 圈数容差 |
| `polygon` | list? | None | 绕行区域多边形（取质心为中心） |
| `center_lat` | float? | None | 绕行中心纬度（与 polygon 二选一） |
| `center_lon` | float? | None | 绕行中心经度 |

**计算逻辑**：

1. 确定绕行中心（polygon 质心或显式坐标）
2. 若有 polygon，过滤掉区域内部的点（仅用外部点分析绕行）
3. 遍历路径点，累计相对中心的方位角变化量
4. counted = |累计角度| / 2π
5. |counted − target_laps| ≤ tolerance → passed = True

---

### 8. `corridor_segment_min_length` — 走廊段落最小长度

**评价理念**：检查路径在走廊缓冲区内的累计长度是否达到最小要求。与 `must_pass_corridor` 的区别在于：本指标统计总累计长度（可以不连续），而非最长连续段。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `corridor` | list | — | 走廊折线 |
| `min_length_m` | float | — | 要求的最小走廊段总长度 |
| `buffer_m` | float | 80.0 | 缓冲区半径 |

**计算逻辑**：

- 统计路径中两端点都在走廊 buffer_m 内的线段的累计长度 buffered_len
- buffered_len ≥ min_length_m → score = 1.0, passed = True
- 否则 → score = buffered_len / min_length_m, passed = False

---

### 9. `prefer_corridor` — 偏好走廊

**评价理念**：评估路径是否偏好经过指定走廊（河流、风景线等）。与 `must_pass_corridor` 的区别在于：本指标是软性偏好（更多地走在走廊附近得更高分），而非硬性要求连续经过。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `corridor` | list | — | 走廊折线 |
| `buffer_m` | float | 150.0 | 缓冲区半径 |
| `min_coverage_ratio` | float | 0.35 | 要求的最小覆盖比例 |

**计算逻辑**：

- coverage = 在 buffer_m 内的路径长度 / 路径总长
- mean_d = 所有点到走廊的平均距离
- score = 0.5 × min(1, coverage / min_coverage) + 0.5 × max(0, 1 − mean_d / (2 × buffer_m))
- coverage ≥ min_coverage × 0.85 → passed = True

---

## 三、指标选用速查表

| 任务模式 | 典型指令 | 推荐指标组合 |
|----------|----------|-------------|
| 点到点 | "从 A 到 B" | waypoint_coverage(硬) |
| 点到点+里程 | "从 A 到 B，约 5km" | waypoint_coverage(硬) + target_distance(软) |
| 往返不重走 | "去 B 再回 A，不要原路" | waypoint_coverage(硬) + no_backtrack(硬) + min/max_distance |
| 原路返回 | "原路走回来" | waypoint_coverage(硬) + require_same_route_return(硬) |
| 绕行一圈 | "绕清华跑一圈" | loop_to_start(硬) + region_penetration(硬) + orbit_uniformity(软) + boundary_proximity(软) + parallel_corridors(软) + reference_distance(软) |
| 多圈绕行 | "绕福海两圈" | loop_to_start(硬) + multi_lap(硬) + region_penetration(硬) |
| 沿走廊 | "沿南长河走 500m" | must_pass_corridor(硬) + corridor_segment_min_length(硬) + corridor_follow_uniformity(软) |
| 偏好走廊 | "沿河边跑" | prefer_corridor(软) |
| 转向偏好 | "尽量少左转" | turn_preference(软) |
| 避开道路 | "避开中关村东路" | avoid_roads(硬) + waypoint_coverage(硬) |
| 避开地点 | "散步避开工地" | avoid_point(硬) |
| 固定起点 | "从北大东门出发" | start_point(硬) |
| 固定终点 | "最终到达军博" | end_point(硬) |
| 园内活动 | "在公园里跑，别出园" | region_penetration(硬, mode=no_exit) |

---

## 四、路径吸附与致密化

部分指标（`avoid_roads` / `no_backtrack_on_return` / `require_same_route_return`）依赖将路径吸附到路网边来获取边属性（way_id、道路名）。被评测的路径点序列往往是稀疏的——相邻两点之间可能跨越很长一段实际道路。若直接取相邻点中点吸附，会丢失中间经过的所有边，导致违规漏检。

为此，吸附前会对路径按固定间距（默认 **10 米**）做**重采样致密化**（`densify_path`）：在每对间隔超过阈值的相邻点之间线性插值补点，使吸附粒度足够细。致密化保留所有原始顶点，只在段内补点，对城市尺度的经纬度而言线性插值误差可忽略。

需要"逐段累加段长"的指标（如 `avoid_roads` 计算违规里程占比）使用 `snap_densified_with_names`，它返回致密化后的点序列与逐段对齐的道路名，保证段名与段长索引一致。

---

## 五、硬约束与软权重

每个指标都有 `hard`（是否硬约束）和 `weight`（权重）两个属性，二者作用不同，容易混淆：

- **`hard`（硬约束）**：决定 pass/fail 是否影响整体封顶。任一硬约束未通过时，综合分被封顶到 `hard_fail_cap`（默认 0.5），或在 `strict_hard=true` 时直接归零。
- **`weight`（权重）**：决定该指标在**加权平均软分**中的占比。所有指标（无论硬软）的 score 按 weight 加权平均得到基础分。

换言之：硬约束的 pass/fail 是"门槛"，weight 是"该项在连续得分里的话语权"。一个指标可以既是硬约束（必须满足，否则封顶）又有较小 weight（在软分里占比不高）。权重无需手动凑成 1，系统会自动归一化。
