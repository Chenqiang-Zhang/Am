# web — 推薦システム フロントエンド (v1)

React + Vite + TypeScript。バックエンド（`../api`）の `POST /recommend` を叩き、
推薦結果を「なぜ推薦されたか」の根拠付きで表示する。

## 前提
- Node.js 18+（推奨 20 LTS）
- バックエンド（FastAPI）が `http://localhost:8000` で起動していること

## 起動

```bash
# ターミナル1 — バックエンド（リポジトリ Am/ 直下で）
uvicorn api.main:app --reload          # → http://localhost:8000

# ターミナル2 — フロントエンド（この web/ で）
npm install
npm run dev                            # → http://localhost:5173
```

dev では `/api` を `:8000` へ proxy する（`vite.config.ts`）。フロントは `/api/recommend` を叩くだけで、CORS 設定は不要。

## 型チェック / ビルド
```bash
npm run build    # tsc 型チェック + 本番ビルド（dist/）
```

## ディレクトリと設計ルール
```
src/
├── types/recommend.ts   # ★ バックエンド契約の唯一の置き場（api/models.py のミラー）
├── api/client.ts        # ★ API 通信の隔離（fetch はここだけ）
├── pages/SearchPage.tsx # 状態管理・API 呼び出しを集約
└── components/          # props を受けて描画するだけ（ロジックを持たない）
```

保守ルール（3 人で守る）:
1. バックエンドのレスポンス形が変わったら `types/recommend.ts` だけを直す。
2. `fetch` を散らさず、必ず `api/client.ts` 経由にする。
3. `components/` にデータ取得や状態を持ち込まない。状態は `SearchPage` に集約。
4. v1 では状態管理ライブラリを入れない（`useState` のみ）。スタイルは CSS Modules。

設計の全体像は [../../progress/UI設計案.md](../../progress/UI設計案.md) を参照。
