const LIMIT = 25_000_000;

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const length = Number(request.headers.get("content-length") ?? 0);
  if (length > LIMIT) return Response.json({ detail: "Uploaded file exceeds the size limit." }, { status: 413 });
  const chunks: Uint8Array[] = [];
  let total = 0;
  const reader = request.body?.getReader();
  if (!reader) return Response.json({ detail: "Choose a CSV or ZIP file." }, { status: 422 });
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > LIMIT) { await reader.cancel(); return Response.json({ detail: "Uploaded file exceeds the size limit." }, { status: 413 }); }
    chunks.push(value);
  }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
  const { id } = await context.params;
  const base = process.env.DATASCOUT_API_URL ?? "http://127.0.0.1:8000";
  const query = new URL(request.url).search;
  try {
    const upstream = await fetch(`${base}/workflows/${id}/upload${query}`, {
      method: "POST",
      headers: { "content-type": "application/octet-stream",
        "x-datascout-filename": request.headers.get("x-datascout-filename") ?? "",
        "x-datascout-source-page": request.headers.get("x-datascout-source-page") ?? "" },
      body, cache: "no-store", signal: AbortSignal.timeout(180000),
    });
    return new Response(await upstream.text(), { status: upstream.status, headers: { "content-type": "application/json" } });
  } catch {
    return Response.json({ detail: "The DataScout API is unavailable. Start the Python service and try again." }, { status: 502 });
  }
}
