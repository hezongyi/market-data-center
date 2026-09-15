import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  createRootRoute,
  createRoute,
  createRouter,
  Link,
  Outlet,
  redirect,
  RouterProvider,
  useRouter,
} from "@tanstack/react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  Database,
  ListChecks,
  LogOut,
  Menu,
  Moon,
  Settings,
  Sun,
  TriangleAlert,
} from "lucide-react";
import { createDataCenterClient } from "./lib/api";
import { Button } from "./components/shadcn/button";
import { Badge } from "./components/shadcn/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "./components/shadcn/card";
import { Input } from "./components/shadcn/input";
import { Label } from "./components/shadcn/label";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  useSidebar,
} from "./components/shadcn/sidebar";
import { TaskDetailPage, TasksPage } from "./p1/tasks";
import "./p1.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 5000, refetchOnWindowFocus: false, retry: 1 },
  },
});
const client = createDataCenterClient("");

function AuthScreen({
  initialized,
  onAuthenticated,
}: {
  initialized: boolean;
  onAuthenticated: (username: string) => void;
}) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(initialized);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (!ready) {
        await client.auth.initialize(username, password);
        setReady(true);
        setPassword("");
      } else {
        const result = await client.auth.login(username, password);
        onAuthenticated(result.data.username);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "认证失败");
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="grid min-h-svh place-items-center bg-muted/40 p-4">
      <Card className="w-full max-w-sm gap-4">
        <CardHeader>
          <div className="mb-4 grid size-10 place-items-center rounded-lg bg-primary text-primary-foreground">
            <Database />
          </div>
          <CardTitle>
            <h1 className="m-0 text-lg">
              {ready ? "登录数据中心" : "初始化管理员"}
            </h1>
          </CardTitle>
          <CardDescription>
            {ready
              ? "使用真实管理员会话进入任务工作台。"
              : "首次使用请设置至少 12 位密码。"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form className="grid gap-4" onSubmit={submit}>
            <div className="field">
              <Label htmlFor="username">用户名</Label>
              <Input
                id="username"
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </div>
            <div className="field">
              <Label htmlFor="password">密码</Label>
              <Input
                id="password"
                type="password"
                autoComplete={ready ? "current-password" : "new-password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            {error && (
              <p
                role="alert"
                className="m-0 flex gap-2 text-sm text-destructive"
              >
                <TriangleAlert className="size-4 shrink-0" />
                {error}
              </p>
            )}
            <Button disabled={busy || password.length < (ready ? 1 : 12)}>
              {busy ? "处理中…" : ready ? "登录" : "设置密码"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}

function Shell({
  username,
  onLogout,
}: {
  username: string;
  onLogout: () => void;
}) {
  const [dark, setDark] = useState(
    () => localStorage.getItem("mdc.theme") === "dark",
  );
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("mdc.theme", dark ? "dark" : "light");
  }, [dark]);
  const preview = import.meta.env.VITE_PREVIEW_ID
    ? {
        id: import.meta.env.VITE_PREVIEW_ID,
        mode: import.meta.env.VITE_PREVIEW_DATA_MODE,
        commit: import.meta.env.VITE_PREVIEW_COMMIT,
        dirty: import.meta.env.VITE_PREVIEW_DIRTY === "true",
      }
    : null;
  const [scheduler, setScheduler] = useState<
    | {
        dispatch_enabled: boolean;
        scheduler: { heartbeat_at: string | null };
      }
    | null
    | undefined
  >(undefined);
  useEffect(() => {
    if (!preview) return;
    let active = true;
    const refresh = () =>
      void client
        .scheduler()
        .then((result) => active && setScheduler(result.data))
        .catch(() => active && setScheduler(null));
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [preview?.id]);
  return (
    <SidebarProvider>
      <Sidebar>
        <SidebarHeader>
          <div className="flex items-center gap-3 px-2 py-1">
            <div className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary text-primary-foreground">
              <Database className="size-5" />
            </div>
            <div>
              <div className="text-sm font-semibold">Market Data Center</div>
              <div className="text-xs text-muted-foreground">
                数据维护工作台
              </div>
            </div>
          </div>
        </SidebarHeader>
        <SidebarContent>
          <SidebarGroupLabel>主导航</SidebarGroupLabel>
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton asChild>
                <Link
                  to="/tasks"
                  search={{ q: "", health: "" }}
                  activeProps={{ className: "bg-accent" }}
                >
                  <ListChecks />
                  数据任务
                </Link>
              </SidebarMenuButton>
            </SidebarMenuItem>
            <SidebarMenuItem>
              <SidebarMenuButton asChild>
                <Link to="/legacy">
                  <Menu />
                  兼容控制台
                </Link>
              </SidebarMenuButton>
            </SidebarMenuItem>
            <SidebarMenuItem>
              <SidebarMenuButton disabled>
                <Settings />
                设置
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarContent>
        <SidebarFooter>
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="max-w-28 truncate">
              {username}
            </Badge>
            <Button
              size="icon"
              variant="ghost"
              aria-label="切换主题"
              onClick={() => setDark((v) => !v)}
            >
              {dark ? <Sun /> : <Moon />}
            </Button>
            <Button
              size="icon"
              variant="ghost"
              aria-label="退出登录"
              onClick={onLogout}
            >
              <LogOut />
            </Button>
          </div>
        </SidebarFooter>
      </Sidebar>
      <main className="min-w-0 flex-1 px-4 py-5 md:px-8 md:py-6">
        <MobileSidebarTrigger />
        {preview && (
          <div className="preview-strip" role="status">
            <strong>
              预览 · {preview.mode === "live" ? "真实源沙箱" : "模拟数据"}
            </strong>
            <span>{preview.id}</span>
            <span>
              {preview.commit?.slice(0, 8)}
              {preview.dirty ? " · dirty" : ""}
            </span>
            <span>{preview.mode}</span>
            <span>
              {scheduler === undefined
                ? "调度状态载入中"
                : scheduler === null
                  ? "调度状态不可用"
                  : `调度：${scheduler.dispatch_enabled ? "有效派发" : "未派发"} · 心跳 ${scheduler.scheduler.heartbeat_at ?? "等待中"}`}
            </span>
          </div>
        )}
        <Outlet />
      </main>
    </SidebarProvider>
  );
}

function MobileSidebarTrigger() {
  const { setMobileOpen } = useSidebar();
  return (
    <Button
      className="mb-4 md:hidden"
      variant="outline"
      onClick={() => setMobileOpen(true)}
    >
      <Menu />
      打开导航
    </Button>
  );
}

function Root() {
  const router = useRouter();
  const [state, setState] = useState<{
    loading: boolean;
    initialized: boolean;
    username: string | null;
    error: string | null;
  }>({ loading: true, initialized: false, username: null, error: null });
  const check = async () => {
    try {
      const status = await client.auth.status();
      try {
        const me = await client.auth.me();
        setState({
          loading: false,
          initialized: status.data.initialized,
          username: me.data.username,
          error: null,
        });
      } catch {
        setState({
          loading: false,
          initialized: status.data.initialized,
          username: null,
          error: null,
        });
      }
    } catch (reason) {
      setState({
        loading: false,
        initialized: false,
        username: null,
        error: reason instanceof Error ? reason.message : "无法连接认证服务",
      });
    }
  };
  useEffect(() => {
    void check();
  }, []);
  if (state.loading)
    return (
      <div className="grid min-h-svh place-items-center text-sm text-muted-foreground">
        正在连接隔离 API…
      </div>
    );
  if (state.error)
    return (
      <main className="grid min-h-svh place-items-center bg-muted/40 p-4">
        <Card className="w-full max-w-md">
          <CardHeader>
            <CardTitle>
              <h1 className="m-0 text-lg">认证服务不可用</h1>
            </CardTitle>
            <CardDescription>{state.error}</CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              onClick={() => {
                setState((value) => ({ ...value, loading: true, error: null }));
                void check();
              }}
            >
              重试连接
            </Button>
          </CardContent>
        </Card>
      </main>
    );
  if (!state.username)
    return (
      <AuthScreen
        initialized={state.initialized}
        onAuthenticated={(username) =>
          setState((s) => ({ ...s, username, error: null }))
        }
      />
    );
  return (
    <Shell
      username={state.username}
      onLogout={() =>
        void client.auth.logout().then(() => {
          queryClient.clear();
          setState((s) => ({ ...s, username: null }));
          void router.navigate({
            to: "/tasks",
            search: { q: "", health: "" },
          });
        })
      }
    />
  );
}

const rootRoute = createRootRoute({ component: Root });
const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: () => {
    throw redirect({ to: "/tasks", search: { q: "", health: "" } });
  },
});
const tasksRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tasks",
  validateSearch: (search: Record<string, unknown>) => ({
    q: typeof search.q === "string" ? search.q : "",
    health: typeof search.health === "string" ? search.health : "",
  }),
  component: TasksPage,
});
const taskRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tasks/$taskId",
  validateSearch: (search: Record<string, unknown>) => ({
    q: typeof search.q === "string" ? search.q : "",
    health: typeof search.health === "string" ? search.health : "",
  }),
  component: TaskDetailPage,
});
const legacyRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/legacy",
  component: () => (
    <div>
      <header className="mb-4">
        <h1 className="m-0 text-2xl font-bold">兼容控制台</h1>
        <p className="text-sm text-muted-foreground">
          旧功能运行在隔离样式容器中，不会覆盖新主线主题。
        </p>
      </header>
      <iframe
        title="兼容控制台"
        src="/legacy.html"
        className="h-[calc(100svh-9rem)] w-full rounded-lg border bg-white"
      />
    </div>
  ),
});
const routeTree = rootRoute.addChildren([
  indexRoute,
  tasksRoute,
  taskRoute,
  legacyRoute,
]);
const router = createRouter({ routeTree });
declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={queryClient}>
    <RouterProvider router={router} />
  </QueryClientProvider>,
);
