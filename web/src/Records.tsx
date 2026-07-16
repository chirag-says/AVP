import { useEffect, useState } from "react";
import { TriangleAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:7860";

interface SessionRow {
  id: string;
  created_at: string;
  status: string;
  patient: any;
  flags: { field: string; reason: string }[];
}

const STATUS_VARIANT: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  completed: "default",
  abandoned: "destructive",
  in_progress: "secondary",
};

/** Abandoned sessions genuinely lack fields; a blank table cell reads as a bug. */
function NotRecorded() {
  return <span className="text-muted-foreground/60 italic">Not recorded</span>;
}

export default function Records() {
  const [rows, setRows] = useState<SessionRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API_BASE}/api/records`)
      .then((r) => r.json())
      .then(setRows)
      .catch((e) => setError(String(e)));
  }, []);

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle className="text-base">Reception records</CardTitle>
        <CardAction>
          <Badge variant="outline">{rows.length} sessions</Badge>
        </CardAction>
      </CardHeader>

      <CardContent className="p-0">
        {error ? (
          <p className="p-6 text-sm text-destructive">Failed to load records: {error}</p>
        ) : rows.length === 0 ? (
          <p className="p-10 text-center text-sm text-muted-foreground">No intake sessions yet.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Time</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Name</TableHead>
                <TableHead>Phone</TableHead>
                <TableHead className="text-right">Flags</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <TableRow key={row.id}>
                  <TableCell className="whitespace-nowrap text-muted-foreground">
                    {new Date(row.created_at).toLocaleString()}
                  </TableCell>
                  <TableCell>
                    <Badge variant={STATUS_VARIANT[row.status] ?? "outline"}>{row.status}</Badge>
                  </TableCell>
                  <TableCell className="font-medium">
                    {row.patient?.personal?.full_name ?? <NotRecorded />}
                  </TableCell>
                  <TableCell>{row.patient?.personal?.phone ?? <NotRecorded />}</TableCell>
                  <TableCell className="text-right">
                    {row.flags?.length ? (
                      <Badge variant="destructive">
                        <TriangleAlert /> {row.flags.length}
                      </Badge>
                    ) : null}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
