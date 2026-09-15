import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cn } from "../../lib/utils";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "./sheet";

type SidebarContextValue = {
  mobileOpen: boolean;
  setMobileOpen: (value: boolean) => void;
};
const SidebarContext = React.createContext<SidebarContextValue | null>(null);
export function SidebarProvider({ children }: { children: React.ReactNode }) {
  const [mobileOpen, setMobileOpen] = React.useState(false);
  return (
    <SidebarContext.Provider value={{ mobileOpen, setMobileOpen }}>
      <div
        data-slot="sidebar-wrapper"
        className="flex min-h-svh w-full bg-background"
      >
        {children}
      </div>
    </SidebarContext.Provider>
  );
}
export function useSidebar() {
  const value = React.useContext(SidebarContext);
  if (!value) throw new Error("useSidebar must be used inside SidebarProvider");
  return value;
}
function SidebarBody({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full w-full flex-col bg-sidebar text-sidebar-foreground">
      {children}
    </div>
  );
}
export function Sidebar({ children }: { children: React.ReactNode }) {
  const { mobileOpen, setMobileOpen } = useSidebar();
  return (
    <>
      <aside
        data-slot="sidebar"
        className="hidden w-64 shrink-0 border-r bg-sidebar md:block"
      >
        <div className="fixed inset-y-0 w-64">
          <SidebarBody>{children}</SidebarBody>
        </div>
      </aside>
      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent className="w-72 p-0 sm:max-w-72">
          <SheetHeader className="sr-only">
            <SheetTitle>导航</SheetTitle>
            <SheetDescription>移动端主导航</SheetDescription>
          </SheetHeader>
          <SidebarBody>{children}</SidebarBody>
        </SheetContent>
      </Sheet>
    </>
  );
}
export function SidebarHeader({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-header"
      className={cn("flex flex-col gap-2 p-3", className)}
      {...props}
    />
  );
}
export function SidebarContent({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-content"
      className={cn(
        "flex min-h-0 flex-1 flex-col gap-2 overflow-auto p-2",
        className,
      )}
      {...props}
    />
  );
}
export function SidebarFooter({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-footer"
      className={cn("flex flex-col gap-2 border-t p-3", className)}
      {...props}
    />
  );
}
export function SidebarGroupLabel({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-group-label"
      className={cn(
        "flex h-8 items-center px-2 text-xs font-medium text-sidebar-foreground/70",
        className,
      )}
      {...props}
    />
  );
}
export function SidebarMenu({
  className,
  ...props
}: React.ComponentProps<"ul">) {
  return (
    <ul
      data-slot="sidebar-menu"
      className={cn("flex w-full flex-col gap-1", className)}
      {...props}
    />
  );
}
export function SidebarMenuItem(props: React.ComponentProps<"li">) {
  return <li data-slot="sidebar-menu-item" className="relative" {...props} />;
}
export function SidebarMenuButton({
  asChild = false,
  isActive = false,
  className,
  ...props
}: React.ComponentProps<"button"> & { asChild?: boolean; isActive?: boolean }) {
  const Comp = asChild ? Slot : "button";
  return (
    <Comp
      data-slot="sidebar-menu-button"
      data-active={isActive}
      className={cn(
        "flex h-9 w-full items-center gap-2 rounded-md p-2 text-left text-sm outline-none hover:bg-accent focus-visible:ring-2 data-[active=true]:bg-accent data-[active=true]:font-medium",
        className,
      )}
      {...props}
    />
  );
}
