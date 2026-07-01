import { useState, useEffect } from "react";

const FALLBACK_RATE = 150;

let _rate: number | null = null;
let _promise: Promise<number> | null = null;

async function fetchUsdToJpy(): Promise<number> {
  try {
    const res = await fetch(
      "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json"
    );
    const data = await res.json();
    const rate = data?.usd?.jpy;
    return typeof rate === "number" && rate > 0 ? Math.round(rate) : FALLBACK_RATE;
  } catch {
    return FALLBACK_RATE;
  }
}

function getOrFetch(): Promise<number> {
  if (_rate !== null) return Promise.resolve(_rate);
  if (_promise !== null) return _promise;
  _promise = fetchUsdToJpy().then((r) => {
    _rate = r;
    return r;
  });
  return _promise;
}

export function useUsdToJpy(): number {
  const [rate, setRate] = useState<number>(FALLBACK_RATE);
  useEffect(() => {
    getOrFetch().then(setRate);
  }, []);
  return rate;
}
