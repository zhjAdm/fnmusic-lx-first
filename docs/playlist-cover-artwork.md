# 歌单封面

使用内置 imagegen 生成，采用统一的复古纸雕、唱片与印刷颗粒风格，无文字或商标。原始 PNG 作为随应用分发的本地资源保存，不依赖在线图床。

| 歌单 | 文件 | 主题 |
| --- | --- | --- |
| 热门推荐 | `fnmusic/static/playlist-hot-v1.png` | 暖橙、放射光线、唱片与音乐律动 |
| 音乐探险 | `fnmusic/static/playlist-explore-v1.png` | 森林绿、蜿蜒路径、远山与唱片 |
| 曲库考古 | `fnmusic/static/playlist-archaeology-v1.png` | 复古棕、旧唱片档案、暖光 |
| 默认歌单 | `fnmusic/static/playlist-default-v1.png` | 深蓝、奶油白、珍藏唱片与红心 |

## 接入方式

三个内置歌单的卡片、详情和批量详情使用同一套 `coverId`。ID 由各图片内容的 MD5 生成，仅作为缓存标识，不用于安全校验；更换图片后重启服务即可产生新 ID。已有公共歌单行为保持原样。

默认歌单封面在接管模式下应用于当前用户的官方歌单列表、详情和批量详情，不创建额外歌单，不写官方封面配置。优先匹配配置的 `DEFAULT_PLAYLIST_GUID`；未配置时按 `PLAYLIST_NAME`（默认“默认歌单”）识别。官方 GUID、歌曲数量及其他元数据保留，其他歌单封面不变。关闭接管后恢复官方原始封面。

`GET/HEAD /music/api/v1/static/cover` 的原有查询参数和路径形式均支持本地封面，沿用飞牛用户身份验证。封面响应为 `image/png`，私有缓存一天。仅按已注册的图片 ID 查找固定本地路径，不从请求参数拼接文件名；未知封面仍使用原有歌曲图片代理或官方转发。图片随 FPK 源码复制，Python 源码包也包含 PNG。

## 生成提示词

四次独立调用均使用内置 imagegen，`transparent_background=false`。前三张使用以下公共提示词与各自主题拼接后生成，默认歌单的完整提示词另列。

公共提示词：

> Create one finished square 1024x1024 music playlist cover artwork, full bleed, no borders, no mockup. A cohesive collectible series of sophisticated tactile cut-paper editorial illustrations with subtle analog risograph grain, sculptural layered paper shadows, bold restrained geometric composition, one vinyl record motif subtly integrated. Premium independent record sleeve art, readable at small 80px thumbnail size, strong central silhouette, sophisticated color blocking, handmade rather than glossy digital look. Absolutely no text, typography, letters, numbers, watermarks, logos, UI, or app icons.

热门推荐：

> Theme: popular music and lively daily discovery. A deep terracotta and burnt orange vinyl record like a setting sun, radiating warm golden paper rays and rhythmic curved sound forms, ivory highlights, ink-dark red shadows. Uplifting, warm, energetic, musical; crisp beautifully balanced minimalist composition.

音乐探险：

> Theme: musical exploration. A winding pale golden path leads into layered emerald and moss-green forest hills, with a vinyl record as a rising moon or compass-like circle at the center. Tiny star-like flecks made of paper, deep forest and petrol teal shadows, mint and cream highlights. Inviting, curious, expansive; crisp beautifully balanced minimalist composition.

曲库考古：

> Theme: rediscovering a treasured personal music archive. A single worn cream vinyl record sleeve half pulled from a beautifully arranged stack of archival records, a dark circular vinyl peeking out, warm paper brown and ochre, muted dusty blue accents, golden late-afternoon light, rich espresso shadows. Nostalgic and intimate, archival discovery, clear object silhouette, crisp beautifully balanced minimalist composition.

默认歌单（完整提示词）：

> Create one finished square 1024x1024 music playlist cover artwork, full bleed, no borders, no mockup. Match a cohesive collectible series of sophisticated tactile cut-paper editorial illustrations with subtle analog risograph grain, sculptural layered paper shadows, bold restrained geometric composition, one vinyl record motif integrated. Premium independent record sleeve art, readable at small 80px thumbnail size, strong central silhouette, sophisticated color blocking, handmade rather than glossy digital look. Absolutely no text, typography, letters, numbers, watermarks, logos, UI, or app icons. Theme: a personal favorite music collection, a cozy musical home to return to. One ivory vinyl sleeve and a dark navy vinyl record resting against a compact stack of cherished records, a small warm red heart as the record's central label. Deep midnight navy and dusty indigo background, creamy paper highlights, small restrained vermilion and muted gold accents. A quiet warm pool of lamplight, a few simple curved paper sound-wave shapes, inviting, intimate, timeless. Dominant navy palette should clearly distinguish this cover from an orange popular-music cover, a forest-green exploration cover, and a brown archival-records cover. Crisp beautifully balanced minimalist composition.
