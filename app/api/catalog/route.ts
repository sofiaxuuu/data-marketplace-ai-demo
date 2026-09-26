export async function GET() {
  const base = process.env.DATASCOUT_API_URL ?? "http://127.0.0.1:8000";
  try {
    const upstream = await fetch(`${base}/catalog`, { cache: "no-store" });
    return new Response(await upstream.text(), {
      status: upstream.status,
      headers: { "content-type": "application/json" },
    });
  } catch {
    return Response.json({ detail: "Catalog unavailable" }, { status: 502 });
  }
}
