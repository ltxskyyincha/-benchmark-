"""
Benchmark 构建平台 — Flask 后端
"""
import json, os, sqlite3, uuid, time
from pathlib import Path
from flask import (Flask, request, jsonify, render_template,
                   send_from_directory, g)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = Path(__file__).parent / 'uploads'
app.config['UPLOAD_FOLDER'].mkdir(exist_ok=True)
DB_PATH = Path(__file__).parent / 'data' / 'platform.db'
DB_PATH.parent.mkdir(exist_ok=True)

# ── 平台数据库 ─────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db: db.close()

def init_db():
    db = sqlite3.connect(str(DB_PATH))
    db.executescript("""
    CREATE TABLE IF NOT EXISTS map_dbs (
        id TEXT PRIMARY KEY, name TEXT NOT NULL,
        filename TEXT, file_path TEXT, uploaded_at REAL
    );
    CREATE TABLE IF NOT EXISTS benchmarks (
        id TEXT PRIMARY KEY, name TEXT NOT NULL,
        description TEXT DEFAULT '', created_at REAL
    );
    CREATE TABLE IF NOT EXISTS test_cases (
        id TEXT PRIMARY KEY, benchmark_id TEXT NOT NULL REFERENCES benchmarks(id) ON DELETE CASCADE,
        map_db_id TEXT REFERENCES map_dbs(id), instruction TEXT DEFAULT '',
        notes TEXT DEFAULT '', sort_order INTEGER DEFAULT 0,
        created_at REAL, updated_at REAL
    );
    CREATE TABLE IF NOT EXISTS geometries (
        id TEXT PRIMARY KEY, test_case_id TEXT NOT NULL REFERENCES test_cases(id) ON DELETE CASCADE,
        name TEXT NOT NULL, geom_type TEXT NOT NULL,
        coordinates TEXT NOT NULL, source_entity_id INTEGER,
        color TEXT DEFAULT '#3388ff'
    );
    CREATE TABLE IF NOT EXISTS metrics (
        id TEXT PRIMARY KEY, test_case_id TEXT NOT NULL REFERENCES test_cases(id) ON DELETE CASCADE,
        metric_type TEXT NOT NULL, params TEXT DEFAULT '{}',
        weight REAL DEFAULT 0.1, is_hard INTEGER DEFAULT 0,
        sort_order INTEGER DEFAULT 0
    );
    """)
    db.commit(); db.close()

init_db()

def uid(): return uuid.uuid4().hex[:12]
def now(): return time.time()
def row_to_dict(row):
    return dict(row) if row else None
def rows_to_list(rows):
    return [dict(r) for r in rows]

# ── 页面路由 ───────────────────────────────────────────────

@app.route('/')
def index_page():
    return render_template('index.html')

@app.route('/workbench/<bid>')
def workbench_page(bid):
    return render_template('workbench.html', benchmark_id=bid)

@app.route('/maps')
def maps_page():
    return render_template('maps.html')

@app.route('/docs')
def docs_page():
    return render_template('docs.html')

# ── 地图 API ──────────────────────────────────────────────

@app.route('/api/maps', methods=['GET'])
def list_maps():
    rows = get_db().execute("SELECT * FROM map_dbs ORDER BY uploaded_at DESC").fetchall()
    return jsonify(rows_to_list(rows))

@app.route('/api/maps/upload', methods=['POST'])
def upload_map():
    f = request.files.get('file')
    if not f or not f.filename.endswith('.db'):
        return jsonify(error="请上传 .db 文件"), 400
    mid = uid()
    # 名称为空（含纯空白）时回退到文件名，避免下拉框无文字
    name = (request.form.get('name') or '').strip()
    if not name:
        name = f.filename.rsplit('.', 1)[0]
    dest = app.config['UPLOAD_FOLDER'] / f"{mid}.db"
    f.save(str(dest))
    get_db().execute(
        "INSERT INTO map_dbs VALUES (?,?,?,?,?)",
        (mid, name, f.filename, str(dest), now()))
    get_db().commit()
    return jsonify(id=mid, name=name)

@app.route('/api/maps/<mid>', methods=['PATCH'])
def rename_map(mid):
    """重命名地图昵称。"""
    d = request.json or {}
    name = (d.get('name') or '').strip()
    if not name:
        return jsonify(error="名称不能为空"), 400
    db = get_db()
    row = db.execute("SELECT id FROM map_dbs WHERE id=?", (mid,)).fetchone()
    if not row:
        return jsonify(error="not found"), 404
    db.execute("UPDATE map_dbs SET name=? WHERE id=?", (name, mid))
    db.commit()
    return jsonify(id=mid, name=name)

@app.route('/api/maps/<mid>', methods=['DELETE'])
def delete_map(mid):
    """删除地图（同时删除磁盘文件）。引用该地图的用例 map_db_id 置空。"""
    db = get_db()
    row = db.execute("SELECT file_path FROM map_dbs WHERE id=?", (mid,)).fetchone()
    if not row:
        return jsonify(error="not found"), 404
    # 解除用例引用
    db.execute("UPDATE test_cases SET map_db_id=NULL WHERE map_db_id=?", (mid,))
    db.execute("DELETE FROM map_dbs WHERE id=?", (mid,))
    db.commit()
    # 删除磁盘文件（容错）
    try:
        fp = row['file_path']
        if fp and os.path.exists(fp):
            os.remove(fp)
    except Exception:
        pass
    return jsonify(ok=True)

@app.route('/api/maps/<mid>/summary')
def map_summary(mid):
    row = get_db().execute("SELECT * FROM map_dbs WHERE id=?", (mid,)).fetchone()
    if not row: return jsonify(error="not found"), 404
    try:
        conn = sqlite3.connect(f"file:{row['file_path']}?mode=ro", uri=True)
        n_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
        n_edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
        n_ents = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        # 兼容新旧 schema：尝试查询 name 列
        n_named_edges = 0
        try:
            n_named_edges = conn.execute(
                "SELECT COUNT(*) FROM graph_edges WHERE name IS NOT NULL"
            ).fetchone()[0]
        except Exception:
            pass
        conn.close()
        return jsonify(name=row['name'], nodes=n_nodes, edges=n_edges,
                       entities=n_ents, named_edges=n_named_edges)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route('/api/maps/<mid>/road_names')
def map_road_names(mid):
    """搜索地图中的道路名称（用于 avoid_roads 指标的路名选择）"""
    row = get_db().execute("SELECT file_path FROM map_dbs WHERE id=?", (mid,)).fetchone()
    if not row: return jsonify(error="not found"), 404
    q = request.args.get('q', '')
    conn = sqlite3.connect(f"file:{row['file_path']}?mode=ro", uri=True)
    try:
        if q:
            rows = conn.execute(
                "SELECT DISTINCT name FROM graph_edges "
                "WHERE name IS NOT NULL AND name LIKE ? ORDER BY name LIMIT 50",
                (f"%{q}%",)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT name FROM graph_edges "
                "WHERE name IS NOT NULL ORDER BY name LIMIT 100"
            ).fetchall()
        names = [r[0] for r in rows]
    except Exception:
        names = []
    conn.close()
    return jsonify(names)

@app.route('/api/maps/<mid>/graph')
def map_graph(mid):
    """
    返回路网图结构，供前端做节点吸附与最短路径计算。

    格式（紧凑，按索引对齐以减小体积）：
      nodes: [[id, lat, lon], ...]
      edges: [[a_id, b_id, length], ...]   无向边
    前端据此构建邻接表跑 Dijkstra。
    """
    row = get_db().execute("SELECT file_path FROM map_dbs WHERE id=?", (mid,)).fetchone()
    if not row:
        return jsonify(error="not found"), 404
    conn = sqlite3.connect(f"file:{row['file_path']}?mode=ro", uri=True)
    nodes = [[r[0], r[1], r[2]]
             for r in conn.execute("SELECT id, lat, lon FROM graph_nodes")]
    node_ids = {n[0] for n in nodes}
    edges = []
    for r in conn.execute("SELECT node_a, node_b, length FROM graph_edges"):
        if r[0] in node_ids and r[1] in node_ids:
            edges.append([r[0], r[1], round(r[2], 2)])
    conn.close()
    return jsonify(nodes=nodes, edges=edges)

@app.route('/api/maps/<mid>/network')
def map_network(mid):
    """路网 GeoJSON（边 → LineString）"""
    row = get_db().execute("SELECT file_path FROM map_dbs WHERE id=?", (mid,)).fetchone()
    if not row: return jsonify(error="not found"), 404
    conn = sqlite3.connect(f"file:{row['file_path']}?mode=ro", uri=True)
    nodes = {}
    for r in conn.execute("SELECT id, lat, lon FROM graph_nodes"):
        nodes[r[0]] = (r[1], r[2])
    features = []
    for r in conn.execute("SELECT node_a, node_b, length, highway, name, ref FROM graph_edges"):
        a, b = nodes.get(r[0]), nodes.get(r[1])
        if a and b:
            props = {"highway": r[3], "length": round(r[2],1)}
            if r[4]: props["name"] = r[4]
            if r[5]: props["ref"] = r[5]
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString",
                             "coordinates": [[a[1],a[0]], [b[1],b[0]]]},
                "properties": props
            })
    conn.close()
    return jsonify(type="FeatureCollection", features=features)

@app.route('/api/maps/<mid>/entities')
def map_entities(mid):
    """实体 GeoJSON"""
    row = get_db().execute("SELECT file_path FROM map_dbs WHERE id=?", (mid,)).fetchone()
    if not row: return jsonify(error="not found"), 404
    q = request.args.get('q', '')
    cat = request.args.get('cat', '')
    conn = sqlite3.connect(f"file:{row['file_path']}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sql = "SELECT id, source_type, source_id, canonical_name, category, geom_type, centroid_lat, centroid_lon, others FROM entities WHERE 1=1"
    params = []
    if q:
        sql += " AND canonical_name LIKE ?"
        params.append(f"%{q}%")
    if cat:
        sql += " AND category LIKE ?"
        params.append(f"{cat}%")
    features = []
    for r in conn.execute(sql, params):
        others = json.loads(r['others']) if r['others'] else {}
        geom = None
        if r['geom_type'] == 'polygon' and 'polygon_lonlat' in others:
            coords = others['polygon_lonlat']
            if len(coords) >= 3:
                geom = {"type": "Polygon", "coordinates": [coords]}
        elif r['geom_type'] == 'polyline' and 'polyline_latlons' in others:
            pts = others['polyline_latlons']
            geom = {"type": "LineString",
                    "coordinates": [[p[1],p[0]] for p in pts]}
        if not geom:
            geom = {"type": "Point",
                    "coordinates": [r['centroid_lon'], r['centroid_lat']]}
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "eid": r['id'], "name": r['canonical_name'] or "",
                "category": r['category'], "geom_type": r['geom_type'],
                "centroid": [r['centroid_lat'], r['centroid_lon']],
            }
        })
    conn.close()
    return jsonify(type="FeatureCollection", features=features)

@app.route('/api/maps/<mid>/entity/<int:eid>')
def map_entity_detail(mid, eid):
    row = get_db().execute("SELECT file_path FROM map_dbs WHERE id=?", (mid,)).fetchone()
    if not row: return jsonify(error="not found"), 404
    conn = sqlite3.connect(f"file:{row['file_path']}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
    conn.close()
    if not r: return jsonify(error="entity not found"), 404
    others = json.loads(r['others']) if r['others'] else {}
    # 提取 latlon 格式的坐标
    coords = None
    if r['geom_type'] == 'polygon' and 'polygon_lonlat' in others:
        coords = [[p[1], p[0]] for p in others['polygon_lonlat']]
    elif r['geom_type'] == 'polyline' and 'polyline_latlons' in others:
        coords = others['polyline_latlons']
    return jsonify(
        eid=r['id'], name=r['canonical_name'], category=r['category'],
        geom_type=r['geom_type'],
        centroid=[r['centroid_lat'], r['centroid_lon']],
        coordinates=coords)

# ── Benchmark API ─────────────────────────────────────────

@app.route('/api/benchmarks', methods=['GET'])
def list_benchmarks():
    return jsonify(rows_to_list(
        get_db().execute("SELECT * FROM benchmarks ORDER BY created_at DESC").fetchall()))

@app.route('/api/benchmarks', methods=['POST'])
def create_benchmark():
    d = request.json or {}
    bid = uid()
    get_db().execute("INSERT INTO benchmarks VALUES (?,?,?,?)",
                     (bid, d.get('name','新评测集'), d.get('description',''), now()))
    get_db().commit()
    return jsonify(id=bid)

@app.route('/api/benchmarks/<bid>', methods=['GET'])
def get_benchmark(bid):
    r = get_db().execute("SELECT * FROM benchmarks WHERE id=?", (bid,)).fetchone()
    if not r: return jsonify(error="not found"), 404
    return jsonify(row_to_dict(r))

@app.route('/api/benchmarks/<bid>', methods=['PUT'])
def update_benchmark(bid):
    d = request.json or {}
    get_db().execute("UPDATE benchmarks SET name=?, description=? WHERE id=?",
                     (d.get('name'), d.get('description',''), bid))
    get_db().commit()
    return jsonify(ok=True)

@app.route('/api/benchmarks/<bid>', methods=['DELETE'])
def delete_benchmark(bid):
    get_db().execute("DELETE FROM benchmarks WHERE id=?", (bid,))
    get_db().commit()
    return jsonify(ok=True)

# ── Test Case API ─────────────────────────────────────────

@app.route('/api/benchmarks/<bid>/cases', methods=['GET'])
def list_cases(bid):
    return jsonify(rows_to_list(
        get_db().execute("SELECT * FROM test_cases WHERE benchmark_id=? ORDER BY sort_order",
                         (bid,)).fetchall()))

@app.route('/api/benchmarks/<bid>/cases', methods=['POST'])
def create_case(bid):
    d = request.json or {}
    cid = uid()
    get_db().execute("INSERT INTO test_cases VALUES (?,?,?,?,?,?,?,?)",
        (cid, bid, d.get('map_db_id'), d.get('instruction',''),
         d.get('notes',''), d.get('sort_order',0), now(), now()))
    get_db().commit()
    return jsonify(id=cid)

@app.route('/api/cases/<cid>', methods=['GET'])
def get_case(cid):
    c = row_to_dict(get_db().execute("SELECT * FROM test_cases WHERE id=?", (cid,)).fetchone())
    if not c: return jsonify(error="not found"), 404
    c['geometries'] = rows_to_list(
        get_db().execute("SELECT * FROM geometries WHERE test_case_id=?", (cid,)).fetchall())
    c['metrics'] = rows_to_list(
        get_db().execute("SELECT * FROM metrics WHERE test_case_id=? ORDER BY sort_order",
                         (cid,)).fetchall())
    for m in c['metrics']:
        m['params'] = json.loads(m['params']) if m['params'] else {}
    return jsonify(c)

@app.route('/api/cases/<cid>', methods=['PUT'])
def update_case(cid):
    d = request.json or {}
    db = get_db()
    db.execute("UPDATE test_cases SET map_db_id=?, instruction=?, notes=?, updated_at=? WHERE id=?",
               (d.get('map_db_id'), d.get('instruction',''), d.get('notes',''), now(), cid))
    # 全量替换 geometries
    if 'geometries' in d:
        db.execute("DELETE FROM geometries WHERE test_case_id=?", (cid,))
        for geo in d['geometries']:
            db.execute("INSERT INTO geometries VALUES (?,?,?,?,?,?,?)",
                (geo.get('id', uid()), cid, geo['name'], geo['geom_type'],
                 json.dumps(geo['coordinates'], ensure_ascii=False),
                 geo.get('source_entity_id'), geo.get('color','#3388ff')))
    # 全量替换 metrics
    if 'metrics' in d:
        db.execute("DELETE FROM metrics WHERE test_case_id=?", (cid,))
        for i, m in enumerate(d['metrics']):
            db.execute("INSERT INTO metrics VALUES (?,?,?,?,?,?,?)",
                (m.get('id', uid()), cid, m['metric_type'],
                 json.dumps(m.get('params',{}), ensure_ascii=False),
                 m.get('weight', 0.1), 1 if m.get('is_hard') else 0, i))
    db.commit()
    return jsonify(ok=True)

@app.route('/api/cases/<cid>', methods=['DELETE'])
def delete_case(cid):
    get_db().execute("DELETE FROM test_cases WHERE id=?", (cid,))
    get_db().commit()
    return jsonify(ok=True)

# ── Export API ────────────────────────────────────────────

@app.route('/api/benchmarks/<bid>/export')
def export_benchmark(bid):
    db = get_db()
    cases = rows_to_list(db.execute(
        "SELECT * FROM test_cases WHERE benchmark_id=? ORDER BY sort_order", (bid,)).fetchall())
    output = []
    map_manifest = {}   # db_path → 平台内原始文件名，便于用户准备评测环境
    export_errors = []  # 收集必填几何/参数缺失，缺失则拒绝导出并指明位置
    for c in cases:
        geos = rows_to_list(db.execute(
            "SELECT * FROM geometries WHERE test_case_id=?", (c['id'],)).fetchall())
        geo_bank = {g['name']: json.loads(g['coordinates']) for g in geos}
        metrics_rows = rows_to_list(db.execute(
            "SELECT * FROM metrics WHERE test_case_id=? ORDER BY sort_order",
            (c['id'],)).fetchall())
        # 获取 map_db 信息
        map_row = db.execute("SELECT * FROM map_dbs WHERE id=?",
                             (c['map_db_id'],)).fetchone() if c['map_db_id'] else None
        # 规范化 db_path：maps/<安全地图名>.db（人类可读、可移植）
        if map_row:
            safe = _safe_slug(map_row['name'])
            db_path = f"maps/{safe}.db"
            map_manifest[db_path] = map_row['filename']
        else:
            db_path = ""
        # 解析几何引用（兼容旧 @geo: 引用；新版坐标已内联）
        exported_metrics = []
        waypoint_coords_cache = None  # 用于自动继承到 backtrack 指标
        for m in metrics_rows:
            params = json.loads(m['params']) if m['params'] else {}
            resolved = {}
            for k, v in params.items():
                if isinstance(v, str) and v.startswith('@geo:'):
                    geo_name = v[5:]
                    resolved[k] = geo_bank.get(geo_name, [])
                else:
                    resolved[k] = v
            # 从 waypoint_coverage 的点组中提取 waypoint_coords 供 backtrack 指标使用
            if m['metric_type'] == 'waypoint_coverage' and 'waypoints' in resolved:
                wps = resolved['waypoints']
                if isinstance(wps, list) and wps:
                    waypoint_coords_cache = []
                    for wp in wps:
                        if isinstance(wp, dict):
                            waypoint_coords_cache.append([wp.get('lat',0), wp.get('lon',0)])
                        elif isinstance(wp, (list, tuple)) and len(wp)>=2:
                            waypoint_coords_cache.append([wp[0], wp[1]])
            # backtrack 类指标自动填充 waypoint_coords
            if m['metric_type'] in ('no_backtrack_on_return','require_same_route_return'):
                if 'waypoint_coords' not in resolved and waypoint_coords_cache:
                    resolved['waypoint_coords'] = waypoint_coords_cache
            # 校验必填几何/参数是否齐全（缺失会导致评测时 factory 报错）
            problems = _validate_metric_params(m['metric_type'], resolved)
            for prob in problems:
                export_errors.append({
                    "case_id": c['id'],
                    "instruction": (c['instruction'] or '')[:40],
                    "metric": m['metric_type'],
                    "problem": prob,
                })
            exported_metrics.append({
                "type": m['metric_type'], "params": resolved,
                "weight": m['weight'], "hard": bool(m['is_hard'])
            })
        entry = {
            "id": c['id'],
            "db_path": db_path,
            "instruction": c['instruction'],
            "evaluator_config": {
                "db_path": db_path,
                "metrics": exported_metrics,
                "hard_fail_cap": 0.5, "strict_hard": False
            },
            "notes": c['notes']
        }
        output.append(entry)
    # 必填几何/参数缺失：拒绝导出，返回 422 + 明确的问题清单
    if export_errors:
        return jsonify({
            "error": "导出失败：部分指标缺少必填的几何或参数",
            "details": export_errors,
        }), 422
    # 若存在地图映射，附在响应头注释里（JSON 不支持注释，放一个特殊键）
    # 用户可据此把平台上传的原始文件重命名为 maps/<safe>.db
    if map_manifest:
        return jsonify({"_map_files": map_manifest, "cases": output}) \
            if request.args.get('with_manifest') else jsonify(output)
    return jsonify(output)


# 各指标必填的几何/参数键（缺失或为空会导致评测时 factory 报错）
_REQUIRED_METRIC_PARAMS = {
    "must_pass_corridor":          [("corridor", "polyline")],
    "corridor_follow_uniformity":  [("corridor", "polyline")],
    "corridor_segment_min_length": [("corridor", "polyline")],
    "prefer_corridor":             [("corridor", "polyline")],
    "region_penetration":          [("polygon", "polygon")],
    "region_orbit_uniformity":     [("polygon", "polygon")],
    "orbit_boundary_proximity":    [("polygon", "polygon")],
    "orbit_parallel_corridors":    [("polygon", "polygon")],
    "multi_lap":                   [("polygon", "polygon")],
    "waypoint_coverage":           [("waypoints", "waypoints")],
    "avoid_roads":                 [("road_names", "road_names")],
    "start_point":                 [("lat", "coord"), ("lon", "coord")],
    "end_point":                   [("lat", "coord"), ("lon", "coord")],
}

def _validate_metric_params(metric_type, params):
    """返回该指标缺失的必填项问题列表（空列表表示通过）。"""
    problems = []
    for key, kind in _REQUIRED_METRIC_PARAMS.get(metric_type, []):
        v = params.get(key)
        if kind == "polyline":
            if not isinstance(v, list) or len(v) < 2:
                problems.append(f"缺少走廊折线「{key}」（需至少 2 个点，请在地图上绘制或从实体选取）")
        elif kind == "polygon":
            if not isinstance(v, list) or len(v) < 3:
                problems.append(f"缺少多边形「{key}」（需至少 3 个顶点，请在地图上绘制或从实体选取）")
        elif kind == "waypoints":
            if not isinstance(v, list) or len(v) < 1:
                problems.append(f"缺少途经点「{key}」（请至少添加 1 个）")
        elif kind == "road_names":
            if not isinstance(v, list) or len(v) < 1:
                problems.append(f"缺少要避开的道路「{key}」（请至少选择 1 条）")
        elif kind == "coord":
            if v is None:
                problems.append(f"缺少坐标「{key}」（请设置起点/终点位置）")
    return problems


def _safe_slug(name: str) -> str:
    """把地图名转为文件名安全的 slug（保留中文，替换空白和危险字符）。"""
    import re
    s = (name or "map").strip()
    # 替换路径分隔符和空白
    s = re.sub(r'[\\/\s]+', '_', s)
    # 去掉文件系统危险字符
    s = re.sub(r'[<>:"|?*]', '', s)
    return s or "map"

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
