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
    name = request.form.get('name') or f.filename.rsplit('.', 1)[0]
    dest = app.config['UPLOAD_FOLDER'] / f"{mid}.db"
    f.save(str(dest))
    get_db().execute(
        "INSERT INTO map_dbs VALUES (?,?,?,?,?)",
        (mid, name, f.filename, str(dest), now()))
    get_db().commit()
    return jsonify(id=mid, name=name)

@app.route('/api/maps/<mid>/summary')
def map_summary(mid):
    row = get_db().execute("SELECT * FROM map_dbs WHERE id=?", (mid,)).fetchone()
    if not row: return jsonify(error="not found"), 404
    try:
        conn = sqlite3.connect(f"file:{row['file_path']}?mode=ro", uri=True)
        n_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
        n_edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
        n_ents = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        conn.close()
        return jsonify(name=row['name'], nodes=n_nodes, edges=n_edges, entities=n_ents)
    except Exception as e:
        return jsonify(error=str(e)), 500

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
    for r in conn.execute("SELECT node_a, node_b, length, highway FROM graph_edges"):
        a, b = nodes.get(r[0]), nodes.get(r[1])
        if a and b:
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString",
                             "coordinates": [[a[1],a[0]], [b[1],b[0]]]},
                "properties": {"highway": r[3], "length": round(r[2],1)}
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
        # 解析几何引用（递归：groups 内嵌套的 "@geo:" 字符串也会被替换）
        def resolve_geo(v):
            if isinstance(v, str) and v.startswith('@geo:'):
                return geo_bank.get(v[5:], [])
            if isinstance(v, list):
                return [resolve_geo(x) for x in v]
            if isinstance(v, dict):
                return {k: resolve_geo(x) for k, x in v.items()}
            return v

        def extract_coords(wp_list):
            """从点列表（dict 或 [lat,lon]）提取 [[lat,lon],...]"""
            out = []
            for wp in wp_list or []:
                if isinstance(wp, dict) and wp.get('lat') is not None:
                    out.append([wp['lat'], wp.get('lon')])
                elif isinstance(wp, (list, tuple)) and len(wp) >= 2:
                    out.append([wp[0], wp[1]])
            return out

        exported_metrics = []
        waypoint_coords_cache = None  # 用于自动继承到 backtrack 指标
        for m in metrics_rows:
            params = json.loads(m['params']) if m['params'] else {}
            resolved = {k: resolve_geo(v) for k, v in params.items()}
            # 从 waypoint_coverage 中提取途经点坐标供 backtrack 指标使用
            # 新格式: groups 的所有阶段点按序展平；旧格式: waypoints 列表
            if m['metric_type'] == 'waypoint_coverage':
                coords = []
                if isinstance(resolved.get('groups'), list):
                    for grp in resolved['groups']:
                        if isinstance(grp, dict):
                            coords.extend(extract_coords(grp.get('points')))
                elif isinstance(resolved.get('waypoints'), list):
                    coords = extract_coords(resolved['waypoints'])
                if coords:
                    waypoint_coords_cache = coords
            # backtrack 类指标自动填充 waypoint_coords
            if m['metric_type'] in ('no_backtrack_on_return','require_same_route_return'):
                if 'waypoint_coords' not in resolved and waypoint_coords_cache:
                    resolved['waypoint_coords'] = waypoint_coords_cache
            exported_metrics.append({
                "type": m['metric_type'], "params": resolved,
                "weight": m['weight'], "hard": bool(m['is_hard'])
            })
        entry = {
            "id": c['id'],
            "db_path": map_row['filename'] if map_row else "",
            "instruction": c['instruction'],
            "evaluator_config": {
                "metrics": exported_metrics,
                "hard_fail_cap": 0.5, "strict_hard": False
            },
            "notes": c['notes']
        }
        output.append(entry)
    return jsonify(output)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
