import { forwardToBackend } from "@/lib/backend";

export async function GET() {
  try {
    const response = await fetch(`${process.env.DATASCOUT_API_URL ?? "http://127.0.0.1:8000"}/benchmark-review`, { cache: "no-store" });
    return new Response(await response.text(), { status: response.status, headers: { "content-type": "application/json" } });
  } catch {
    return Response.json({ detail: "Benchmark review unavailable" }, { status: 502 });
  }
}

export async function POST(request: Request) {
  return forwardToBackend("/benchmark-review", request);
}
