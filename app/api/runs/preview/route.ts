import { forwardToBackend } from "@/lib/backend";

export async function POST(request: Request) {
  return forwardToBackend("/runs/preview", request);
}
