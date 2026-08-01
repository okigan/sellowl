import { useCallback, useEffect, useRef, useState } from "react";
import type { Health, Item, JobStatus } from "./api";
import { api, money, STAGE_COPY } from "./api";
import { ItemRow } from "./components/ItemRow";
import { SkeletonRow } from "./components/Bits";

const POLL_MS = 2000;
const THEME_KEY = "sellowl-theme";

type Theme = "dark" | "light";

function initialTheme(): Theme {
  const stored = window.localStorage.getItem(THEME_KEY);
  if (stored === "dark" || stored === "light") return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function ThemeToggle({ theme, onToggle }: { theme: Theme; onToggle: () => void }) {
  return (
    <button
      onClick={onToggle}
      title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
      aria-label="Toggle color theme"
      className="rounded-md border border-line px-2.5 py-2 text-[13px] text-muted transition hover:text-text"
    >
      {theme === "dark" ? "☀️" : "🌙"}
    </button>
  );
}

export default function App() {
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [health, setHealth] = useState<Health | null>(null);
  const [storeUrl, setStoreUrl] = useState("");
  const [metro, setMetro] = useState("austin");
  const [job, setJob] = useState<JobStatus | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [error, setError] = useState("");
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    window.localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  useEffect(() => {
    api
      .health()
      .then((h) => {
        setHealth(h);
        setStoreUrl(h.default_store_url);
        setMetro(h.metro);
      })
      .catch(() => setHealth(null));
    return () => window.clearTimeout(timer.current);
  }, []);

  const poll = useCallback(async (id: string) => {
    try {
      const status = await api.job(id);
      setJob(status);
      // Stream rows in as they land rather than blocking on the whole job.
      if (status.item_count > 0) {
        setItems(await api.items(id));
      }
      if (status.status === "done" || status.status === "failed") {
        if (status.error) setError(status.error);
        return;
      }
      timer.current = window.setTimeout(() => void poll(id), POLL_MS);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  const start = useCallback(async () => {
    setError("");
    setItems([]);
    setJob(null);
    try {
      const { job_id } = await api.analyze(storeUrl, metro);
      void poll(job_id);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [storeUrl, metro, poll]);

  const running = job !== null && (job.status === "queued" || job.status === "running");
  const totalOpportunity = items.reduce(
    (sum, item) =>
      sum +
      (item.verdict?.kind === "underpriced" ? Math.max(0, item.verdict.opportunity_usd ?? 0) : 0),
    0,
  );

  return (
    <div className="mx-auto max-w-6xl px-5 py-8">
      <header className="flex items-start justify-between gap-4 pb-6">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">
            <span aria-hidden>🦉</span> SellOwl
          </h1>
          <p className="pt-1 text-[13px] text-muted">
            Paste an eBay store link. Find out what you&rsquo;re leaving on the table.
          </p>
        </div>
        <ThemeToggle
          theme={theme}
          onToggle={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
        />
      </header>

      <div className="flex flex-wrap items-center gap-2 pb-2">
        <input
          value={storeUrl}
          onChange={(e) => setStoreUrl(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !running && void start()}
          placeholder="https://www.ebay.com/usr/…"
          className="min-w-[22rem] flex-1 rounded-md border border-line bg-panel px-3 py-2 text-[13px] outline-none placeholder:text-muted/50 focus:border-overlay-focus"
        />
        <input
          value={metro}
          onChange={(e) => setMetro(e.target.value)}
          placeholder="metro"
          title="Facebook Marketplace metro — local comps come from here"
          className="w-32 rounded-md border border-line bg-panel px-3 py-2 text-[13px] outline-none focus:border-overlay-focus"
        />
        <button
          onClick={() => void start()}
          disabled={running || !storeUrl}
          className="rounded-md bg-accent-surface px-4 py-2 text-[13px] font-medium text-accent-text transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {running ? "Working…" : "Analyze"}
        </button>
      </div>

      {health && (
        <p className="pb-5 text-[11px] text-muted/60">
          {health.apify_configured ? "apify ✓" : "apify ✗ (set APIFY_TOKEN)"} ·{" "}
          {health.elastic_configured ? "elastic ✓" : "elastic ✗ (in-memory matching)"} ·{" "}
          {health.vision_configured ? "vision ✓" : "vision ✗ (title-only matching)"}
        </p>
      )}

      {error && (
        <div className="mb-5 rounded-md border border-bad/30 bg-bad/10 px-3 py-2 text-[12px] text-bad">
          {error}
        </div>
      )}

      {job && (
        <div className="flex items-center gap-3 pb-3 text-[12px] text-muted">
          <span>{STAGE_COPY[job.stage] ?? job.stage}</span>
          {job.total > 0 && (
            <span className="text-muted/60">
              {job.done} of {job.total}
            </span>
          )}
          {running && (
            <span className="h-1 w-24 overflow-hidden rounded-full bg-line">
              <span
                className="block h-full bg-overlay-strong transition-all duration-500"
                style={{ width: `${job.total ? (job.done / job.total) * 100 : 15}%` }}
              />
            </span>
          )}
          {items.length > 0 && totalOpportunity > 0 && (
            <span className="ml-auto text-money">
              {money(totalOpportunity)} left on the table across {items.length} listings
            </span>
          )}
        </div>
      )}

      {(items.length > 0 || running) && (
        <div className="overflow-x-auto rounded-lg border border-line">
          <table className="w-full min-w-[52rem] border-collapse">
            <thead>
              <tr className="border-b border-line bg-panel text-left text-[10px] uppercase tracking-wide text-muted/70">
                <th className="px-3 py-2 font-medium">Listing</th>
                <th className="px-3 py-2 font-medium">You ask</th>
                <th className="px-3 py-2 font-medium">Sold band</th>
                <th className="px-3 py-2 font-medium">Target</th>
                <th className="px-3 py-2 font-medium">Where</th>
                <th className="px-3 py-2 text-right font-medium">Opportunity</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, i) => (
                <ItemRow key={item.external_id} item={item} index={i} />
              ))}
              {running &&
                Array.from({ length: Math.max(0, 3 - items.length) }).map((_, i) => (
                  <SkeletonRow key={`sk-${i}`} />
                ))}
            </tbody>
          </table>
        </div>
      )}

      {!job && !error && (
        <p className="pt-10 text-center text-[12px] text-muted/50">
          Three sources, joined on meaning: your store, eBay sold prices, and local Marketplace
          asks.
        </p>
      )}

      {items.length > 0 && (
        <p className="pt-4 text-[11px] text-muted/50">
          Sold bands are eBay completed sales; local figures are asking prices, not sales. Fees and
          shipping are estimates. Click any row to see the comps behind the number.
        </p>
      )}
    </div>
  );
}
