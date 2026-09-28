import { forwardToBackend } from "@/lib/backend";

export async function GET(request: Request) { return forwardToBackend("/conversations", request); }
export async function POST(request: Request) { return forwardToBackend("/conversations", request); }
