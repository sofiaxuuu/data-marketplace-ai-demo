"use client";

import { Suspense } from "react";
import { useParams, useSearchParams } from "next/navigation";
import ConversationChat from "../../conversation-chat";
import SiteHeader from "../../site-header";
import { useCatalog } from "../../use-catalog";

export default function SavedConversationPage() {
  return <Suspense fallback={<main className="shell"><SiteHeader active="analyze" /><p>Loading conversation…</p></main>}><ConversationPageClient /></Suspense>;
}

function ConversationPageClient() {
  const { id } = useParams<{ id: string }>();
  const search = useSearchParams();
  const { products, error } = useCatalog();
  return <main className="shell"><SiteHeader active="analyze" />
    {error && <p role="alert" className="notice error">{error}</p>}
    <ConversationChat id={id} products={products} initialQuestion={search.get("q") ?? ""} />
  </main>;
}
