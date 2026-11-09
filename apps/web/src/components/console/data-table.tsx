import type { ComponentProps, ReactNode } from "react";
import { Skeleton, Table as HeroTable } from "@heroui/react";
import { cn } from "../../lib/utils";

type ColumnPresentation = { title: ReactNode; key?: string; width?: number };
/** 字段名与渲染参数成对约束，接口字段变化时由类型检查发现受影响的列。 */
export type TableColumn<T> = ColumnPresentation &
  (
    | {
        [K in keyof T]-?: {
          dataIndex: K;
          render?: (value: T[K], record: T, index: number) => ReactNode;
        };
      }[keyof T]
    | {
        dataIndex?: undefined;
        render?: (value: undefined, record: T, index: number) => ReactNode;
      }
  );

/** 表格导航和表面使用 HeroUI，业务列仍保留精确字段类型与独立横向滚动。 */
export function Table<T extends object>({
  columns,
  dataSource,
  loading,
  rowKey,
  className,
  variant,
}: {
  columns: TableColumn<T>[];
  dataSource: T[];
  rowKey: keyof T;
  loading?: boolean;
  pagination?: false;
  className?: string;
  variant?: ComponentProps<typeof HeroTable>["variant"];
}) {
  return (
    <HeroTable
      variant={variant}
      className={cn("console-table", className)}
      aria-busy={!!loading}
    >
      <HeroTable.ScrollContainer>
        <HeroTable.Content aria-label="数据列表">
          <HeroTable.Header>
            {columns.map((column, index) => (
              <HeroTable.Column
                id={String(index)}
                key={index}
                isRowHeader={index === 0}
                {...(column.width ? { style: { width: column.width } } : {})}
              >
                {column.title}
              </HeroTable.Column>
            ))}
          </HeroTable.Header>
          <HeroTable.Body
            renderEmptyState={() => (
              <span className="table-empty">暂无记录</span>
            )}
          >
            {loading && !dataSource.length
              ? Array.from({ length: 4 }, (_, row) => (
                  <HeroTable.Row id={`loading-${row}`} key={`loading-${row}`}>
                    {columns.map((_, index) => (
                      <HeroTable.Cell key={index}>
                        <Skeleton className="h-4 w-3/4" />
                      </HeroTable.Cell>
                    ))}
                  </HeroTable.Row>
                ))
              : dataSource.map((record, index) => (
                  <HeroTable.Row
                    id={String(record[rowKey] ?? index)}
                    key={String(record[rowKey] ?? index)}
                    textValue={String(record[rowKey] ?? index)}
                  >
                    {columns.map((column, columnIndex) => {
                      const value = column.dataIndex
                        ? record[column.dataIndex]
                        : undefined;
                      return (
                        <HeroTable.Cell
                          key={columnIndex}
                          data-label={
                            typeof column.title === "string"
                              ? column.title
                              : undefined
                          }
                        >
                          {/* 调用端约束了字段与渲染器，遍历联合列时需恢复该对应关系。 */}
                          {column.render
                            ? column.render(value as never, record, index)
                            : value == null
                              ? "—"
                              : String(value)}
                        </HeroTable.Cell>
                      );
                    })}
                  </HeroTable.Row>
                ))}
          </HeroTable.Body>
        </HeroTable.Content>
      </HeroTable.ScrollContainer>
    </HeroTable>
  );
}
