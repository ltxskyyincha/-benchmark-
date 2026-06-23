# Benchmark 协作构建平台

## 快速启动

```bash
pip install flask
python app.py
# 浏览器打开 http://localhost:5000
```

## 使用流程

1. **地图管理** → 上传 preprocess.py 生成的 `.db` 文件
2. **首页** → 创建评测集
3. **工作台** → 选择地图 → 新建用例 → 编写指令 → 配置指标 → 保存
4. **导出** → 点击 ⬇ 按钮导出标准 `benchmark.json`

## 几何对象库

在工作台左下角管理几何对象（多边形/折线）:
- **⬡ 画多边形**: 在地图上点击绘制
- **〰 画折线**: 在地图上点击绘制
- **🎯 选实体**: 点击地图上的 DB 实体捕获其几何

指标配置中通过下拉框引用几何库对象，导出时自动解析为坐标。

## 目录结构

```
platform/
├── app.py              Flask 后端（API + 页面路由）
├── static/
│   ├── css/style.css   样式
│   └── docs.md         指标文档
├── templates/
│   ├── base.html       基础模板
│   ├── index.html      评测集列表
│   ├── workbench.html  核心工作台（地图 + 编辑器）
│   ├── maps.html       地图管理
│   └── docs.html       在线文档
├── uploads/            上传的 .db 文件
└── data/
    └── platform.db     平台自身数据
```
