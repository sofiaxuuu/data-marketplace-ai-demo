"use client";

import { useEffect, useState } from "react";
import type { InspectProduct } from "./candidate-inspector";

export function useCatalog() {
  const [products, setProducts] = useState<InspectProduct[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    const refresh = () => fetch("/api/catalog", { cache: "no-store" })
      .then(response => {
        if (!response.ok) throw new Error("Catalog unavailable");
        return response.json();
      })
      .then((items: InspectProduct[]) => {
        if (!active) return;
        setProducts(previous => JSON.stringify(previous) === JSON.stringify(items) ? previous : items);
        setError("");
      })
      .catch(() => {
        if (!active) return;
        setProducts(previous => previous.length ? [] : previous);
        setError("Catalog unavailable. Check that the Python API is running, then try again.");
      });
    refresh();
    const timer = window.setInterval(refresh, 30000);
    window.addEventListener("focus", refresh);
    return () => { active = false; window.clearInterval(timer); window.removeEventListener("focus", refresh); };
  }, []);

  return { products, error };
}
