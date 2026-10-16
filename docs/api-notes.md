# CarbonSpace API 笔记（第 0 步）

> 更新：2026-10-14。API version `1.0.266.0`。✅ = 已实测，❓ = 未验证。
> 实测账号：一个拥有 UW Library 写权限的 Unknown Works 账号。

## 1. 认证 —— ⚠️ 关键限制

| 方式 | 读 | 写 |
|---|---|---|
| `X-API-KEY: email\|key` | ✅ 200 | ❌ **401**（`www-authenticate: Bearer`） |
| `Authorization: Bearer <Auth0 access token>` | ✅ | ✅ 200（**有效期 24 小时**，scope `openid profile email`） |

**获取 Bearer token 的方法**：登录 public.carbonspace.earth → F12 打开 Console，运行
`copy(await [...document.querySelectorAll('*')].find(e=>e.__vue_app__).__vue_app__.config.globalProperties.$auth0.getAccessTokenSilently())`
token 会被复制到剪贴板，工具再从剪贴板读取。这一操作之后可以做成书签。

- 已测写接口 `Calculation/Copy`、`Calculation/Create`、`Calculation/CreateOrUpdateCalculation` 用 API key 全部 401 → **API key 是只读的**（官方文档也只把它用于 PowerBI 读数）。
- Auth0：tenant `carbonscape-sandbox.eu.auth0.com`，audience `https://api.carbonscape.io`，公开 client `g9Rl7PVG0IPqxi61j6bdTSV26nwJacpc`（`GET /api/User/GetOAuthClient`，网页和 Swagger 共用）。
  - ✅ device code 流程：该 client **不允许**（`unauthorized_client`）
  - implicit / authorization_code：回调地址限定在 Swagger 和网页，本地工具无法接收
- Swagger 登录需勾选 `openid profile email` scope，否则 GetMe 返回 403 "User not found"。
- **网页 UI 没有生成 API key 的入口**，只能用 `GET /api/User/GenerateApiKey?id=<UserId>`。

## 2. 项目（✅ `Project/GetAll`）

| Id | 名称 | 说明 | 权限 |
|---|---|---|---|
| 154 | CarbonSpace Materials | `IsLibrary`, `IsDefaultLibrary`；基础材料（Simple/Nested，m3/kg） | 只读 |
| 155 | CarbonSpace Components | 默认库；做法/构件（m2 等） | 只读 |
| 156 | Okobaudat 2024 | 库；EPD 原始数据（大量德语名） | 只读 |
| **1302** | **UW Library** | 公司组件库，已有 Project A 墙体做法、Project B 木材等 | 读写 |
| 1303 | Project A | 实际项目，计算 1307 "Example calc"（36 行） | 读写 |
| 1646 | UW API Test | 测试项目，计算 1915 "Test 01" | 读写 |

## 3. 数据模型（✅ 由计算 1307 / 1915 反推）

```
Calculation
 ├─ CalculationProperties（inventory）  FullKey: building.gfa / building.footprint / building.height / building.volume …
 └─ CalculationGroup[]  两级树：Superstructure / Substructure / Skin / Space Plan / Services → 子组
      └─ CalculationGroupComponent[]   ← 一行 = 一个组件 × 数量
            ComponentId   可直接引用任意可读项目里的组件（154/155/156/1302）✅ 不需要 Duplicate
            Quantity      = Expression 的值；网页手填时 Expression 就是数字字符串（"95.1"）
            OverrideName  同一组件出现多次时用来区分（例如 "High Level Glazing"）
```
- `ComponentRelation` 只用于组件内部的做法嵌套（例如 "Project A WT.01a" = Nested，m2），与计算本身无关。
- 计算的默认分组就是 CarbonSpace 的 shearing layer 模板（1915 的 20 个组）。
- **单位**：`ComponentUnitType` 在数据里几乎都是 0（Undefined），**要以 `ComponentUnit` 文本为准**（`m3` / `m2` / `kg` / `pcs` / `null`）。
- 组件对象和计算对象上存的 `*RangeValue` 都是 0，**结果不存储，只在请求时计算**。

### 同事目前的实际做法（Project A 计算 1307）
- 墙、屋面、楼板等**按做法类型**录入：在 UW Library 建 Nested 组件（如 `Project A WT.01a`，m2），计算里填面积。
- 结构木材、混凝土、碎石等**按材料**录入：引用 Simple 或库组件（m3 / kg）。
- 也就是说，实际数据是"做法 m2 + 材料 m3"混合录入，不全是按 Revit 材料逐项录。

## 4. 搜索（✅）
- `GET /api/Search/SearchComponentByText/{q}?projects=` 最多返回 **100 条**，`Name` / `Path` 里带 `<b>…</b>` 高亮，需要去掉。
- `Path` 是库里的分类路径，例如 `Minerals > Concrete`、`10 Superstructure > 11 Structural Systems > Concrete`。
- 英文搜索主要命中 154/155；Okobaudat 多为德语名（搜 "Beton" 能命中）。

## 5. 结果（✅）
- `GET /api/Bom/GetByCalculation/{id}`：`CalculationLines[]` 按 EPD 展开（1307 有 36 行输入 → 110 行），每行有：
  `TopComponentName, TopQuantity, TopComponentUnit, EpdComponentName, EpdQuantity, Weight, MaterialCategory, MainShearingLayer, SubShearingLayer, PathNames[], CO2A/B/C/D, CO2Biogenic`
  - 1307 的 A+B+C 合计（数值略）（需与网页显示值核对 ❓）
  - `CalculationProperties` 里有 GFA 等参数 → 可算 kgCO2e/m²
- → `report` / `compare` **只需要这一个接口**，而且用只读 API key 就能调。

## 6. 写入路径（✅ 2026-10-14 用 Bearer token 在 1646 实测；临时计算 2198 / 2199 已删除）

| 操作 | 结果 |
|---|---|
| `POST CreateOrUpdateCalculation`（不带 CalculationId） | ✅ 新建计算，**自动带上默认 shearing layer 分组**；`GroupName` 与已有分组同名时挂到该组，不存在时**自动新建分组** |
| `POST CreateOrUpdateCalculation`（带 CalculationId） | ✅ **整体同步**：已有组件原行更新（行 id 不变），新组件新增，**请求中没有的组件会被删除** |
| `CreateOrUpdateCalculation` 带 `Props` | ❌ 500（服务器 NullReference）→ **不要用 Props** |
| `PATCH UpdateInventoryDictionary/{id}` `{"building.gfa":"456"}` | ✅ |
| `POST Calculation/Copy/{id}?targetProject=&name=` | ✅ 复制分组和全部行 |
| `POST Hierarchy/FetchHierarchyResults {Calculations:[id]}` | ✅ 返回 A/B/C/D（`Co2EmbodiedRangeValue` 为 0，不要读这个字段） |
| `DELETE Calculation/Delete/{id}` | ✅ |

注意：
- 刚写入时 `GetCalculationSetById` 里各行的 `Quantity` 读出来是 0，BOM 合计也是 0；稍后再读 BOM 就正确了（20 m³ 混凝土等合计 9494 kgCO2e，与按单位用量估算的结果一致）。说明**服务器是异步重算的**，push 之后需要等一下或轮询，再读结果。
- 判断"同一行"是按 ComponentId 来的。❓ 同一组件出现在两个分组里、或在同一分组里出现两次（例如 1307 里的 glazing）时如何处理，还需要再测。

### 第二轮实测（临时计算 2200–2202，均已删除）
- `CreateOrUpdateCalculation` 支持同一组件出现在不同分组里，也支持在同一分组里出现两次，各占一行。
- 如果只提交部分行，**其余行全部被删除，包括带 OverrideName 的手动行** → 工具不能用它来更新已有计算。
- 逐行接口：`CalculationGroupComponent/Create` ✅；`Update?fields=Expression&fields=Comment` ✅；`fields=Quantity` ❌ 400（不允许修改该字段）；`Delete` ✅；`CalculationGroup/Create` ✅
- **数量的计算方式**：新建的行 `Quantity` 都是 0，直到 (a) 该行的 Expression 被修改，或 (b) 调用了 `UpdateInventoryDictionary`（会重新计算整个计算）。用 `{"building.gfa": <原值>}` 重写一次即可触发。
- Expression 里可以引用参数，例如 `[building.gfa]*0.5` ✅

### 工具实际采用的写入流程（cs_sync，已在 1646 端到端测试通过）
1. 新建：`CreateOrUpdateCalculation`（传空行列表，得到默认分组）或 `Copy` 模板
2. 逐行 diff：Create / Update(Expression, Comment) / Delete。只删除 Comment 以 `cs_sync:` 开头的行，而且必须加 `--prune`。
3. `UpdateInventoryDictionary` 写回原 GFA，触发重算
4. 读取 BOM，检查每一行的 TopQuantity 是否等于推送的值

### 推荐写入流程（初稿，已被上面的流程取代）
1. 没有 `--calc`：用 `CreateOrUpdateCalculation` 新建，并传入全部行（或者先 `Copy` 公司模板，再用 `CreateOrUpdateCalculation` 同步）。
2. 有 `--calc`：先读现有行，与本次要写入的内容做 diff 并预览，**特别标出将被删除的行**，确认后再用 `CreateOrUpdateCalculation` 同步。
3. GFA 等参数用 `UpdateInventoryDictionary` 写入。
