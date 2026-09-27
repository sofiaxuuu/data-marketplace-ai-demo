import { forwardToBackend } from "@/lib/backend";
export async function POST(request: Request) {
  return forwardToBackend("/sql-runs/generate", request);
}
