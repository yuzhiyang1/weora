import { Card } from "@heroui/react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, FileText, Folder, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { useState } from "react";

import { Button, Empty, Skeleton } from "../../components/console";
import { AssetLoadError } from "../../components/console/asset-controls";
import { MarkdownContent } from "../../components/MarkdownContent";
import { previewSkillFile, type SkillFile, type SkillFileSource } from "./api";

type FolderNode = {
  directories: Map<string, FolderNode>;
  files: SkillFile[];
};

/** 清单里的路径已经标准化，按目录组织供读者逐级浏览。 */
function fileTree(files: SkillFile[]): FolderNode {
  const root: FolderNode = { directories: new Map(), files: [] };
  for (const file of files) {
    let directory = root;
    for (const name of file.path.split("/").slice(0, -1)) {
      let child = directory.directories.get(name);
      if (!child) {
        child = { directories: new Map(), files: [] };
        directory.directories.set(name, child);
      }
      directory = child;
    }
    directory.files.push(file);
  }
  return root;
}

type FileTreeProps = {
  node: FolderNode;
  selected: string;
  onSelect: (path: string) => void;
};

/** 文件夹可独立展开，当前文件在所有层级使用同一选中状态。 */
function FileTree({ node, selected, onSelect }: FileTreeProps) {
  return <ul className="skill-file-tree">
    {node.files.map((file) => <li key={file.path}>
      <Button aria-current={file.path === selected}
        className="skill-file-button" icon={<FileText />} size="small"
        theme={file.path === selected ? "light" : "borderless"} onClick={() => onSelect(file.path)}>
        <span title={file.path}>{file.path.split("/").at(-1)}</span>
      </Button>
    </li>)}
    {[...node.directories].map(([name, child]) =>
      <FileFolder key={name} name={name} node={child} selected={selected} onSelect={onSelect} />)}
  </ul>;
}

/** 文件夹按钮保持键盘可达，并报告其展开状态。 */
function FileFolder({ name, ...tree }: FileTreeProps & { name: string }) {
  const [expanded, setExpanded] = useState(true);
  return <li>
    <Button aria-expanded={expanded} className="skill-file-button"
      icon={expanded ? <ChevronDown /> : <ChevronRight />} size="small" theme="borderless"
      onClick={() => setExpanded(!expanded)}>
      <Folder size={14} aria-hidden="true" />
      <span>{name}</span>
    </Button>
    {expanded && <FileTree {...tree} />}
  </li>;
}

/** 默认阅读 SKILL.md，切换包来源时由调用方重建选中路径和原文模式。 */
export function SkillFileBrowser({ workspaceId, skillId, source, files }: {
  workspaceId: string;
  skillId: string;
  source: SkillFileSource;
  files: SkillFile[];
}) {
  const [path, setPath] = useState(files.find((file) => file.path === "SKILL.md")?.path ?? files[0]?.path ?? "");
  const [raw, setRaw] = useState(false);
  const [desktopFiles, setDesktopFiles] = useState(true);
  const [mobileFiles, setMobileFiles] = useState(false);
  const preview = useQuery({
    queryKey: ["skill-file", workspaceId, skillId, source, path],
    queryFn: ({ signal }) => previewSkillFile(workspaceId, skillId, source, path, signal),
    enabled: !!path,
  });
  const markdown = /\.md(?:own)?$/i.test(path);
  const text = preview.data?.text;
  // 说明正文默认省略 frontmatter，原文模式保留完整文件。
  const body = path === "SKILL.md" && text ? text.replace(/^---[^\S\r\n]*\r?\n[\s\S]*?^---[^\S\r\n]*\r?\n?/m, "") : text;

  if (!path) return <Empty title="该包没有文件" />;
  return <Card className="skill-reader">
    <div className="skill-mobile-file-toggle">
      <Button size="small" aria-expanded={mobileFiles} icon={<Folder />} onClick={() => setMobileFiles(!mobileFiles)}>
        浏览文件
      </Button>
    </div>
    <div className="skill-reader-layout" data-desktop-files={desktopFiles} data-mobile-files={mobileFiles}>
      <nav className="skill-file-nav" aria-label="Skill 文件">
        <FileTree node={fileTree(files)} selected={path} onSelect={(nextPath) => {
          setPath(nextPath);
          setRaw(false);
          setMobileFiles(false);
        }} />
      </nav>
      <div className="skill-reading-pane">
        <div className="skill-file-toolbar">
          <Button className="skill-desktop-file-toggle" size="small" theme="borderless"
            aria-label={desktopFiles ? "收起文件目录" : "展开文件目录"}
            aria-expanded={desktopFiles}
            icon={desktopFiles ? <PanelLeftClose /> : <PanelLeftOpen />}
            onClick={() => setDesktopFiles(!desktopFiles)} />
          <code title={path}>{path}</code>
          {markdown && text != null && <Button size="small" theme="borderless" aria-pressed={raw} onClick={() => setRaw(!raw)}>
            {raw ? "返回预览" : "查看原文"}
          </Button>}
        </div>
        <div className="skill-file-content">
          {preview.isPending ? <Skeleton active />
            : preview.isError ? <AssetLoadError title="文件暂时无法打开"
              description={preview.error.message} onRetry={() => void preview.refetch()} />
              : text === null ? <Empty title="此文件无法预览" description="可以下载完整包查看二进制文件或大文件。" />
                : markdown && !raw ? <MarkdownContent>{body || ""}</MarkdownContent>
                  : <pre aria-label="文件原文" tabIndex={0}>{text}</pre>}
        </div>
      </div>
    </div>
  </Card>;
}
