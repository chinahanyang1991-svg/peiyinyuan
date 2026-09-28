"""
MinerU 文档解析 API 路由
提供 PDF/Office 文件上传 + MinerU 解析端点
"""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from backend.config import Config

router = APIRouter()


@router.post("/parse", summary="使用 MinerU 解析文档")
async def parse_with_mineru(
    file: UploadFile = File(..., description="PDF/Office 文件"),
    output_dir: Optional[str] = Form(None, description="结果输出目录"),
    backend: str = Form("pipeline", description="MinerU 后端模式"),
):
    """
    上传 PDF/Office 文件，通过 MinerU 引擎解析

    支持的格式: PDF, DOCX, PPTX, XLSX
    """
    # 验证文件格式
    ext = Path(file.filename).suffix.lower()
    if ext not in (".pdf", ".docx", ".pptx", ".xlsx"):
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {ext}，仅支持 PDF/DOCX/PPTX/XLSX",
        )

    # 保存上传文件
    upload_dir = Config.DATA_DIR / "uploads" / "mineru"
    upload_dir.mkdir(parents=True, exist_ok=True)

    save_path = upload_dir / file.filename
    try:
        content = await file.read()
        with open(save_path, "wb") as f:
            f.write(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文件保存失败: {e}")

    # 调用 MinerU API
    try:
        from modules.mineru_tools.mineru_api_client import MinerUApiClient

        client = MinerUApiClient()

        # 健康检查
        try:
            health = client.health_check()
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="MinerU 服务未运行，请先启动 MinerU 后端（docker-compose --profile mineru up -d）",
            )

        # 提交解析任务
        options = {"backend": backend} if backend else None
        result = client.submit_and_wait(
            str(save_path),
            output_dir=output_dir,
            options=options,
            max_wait=300.0,
        )

        return {
            "status": "success",
            "file_name": file.filename,
            "format": ext.lstrip("."),
            "result": result,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MinerU 解析失败: {e}")


@router.get("/health", summary="MinerU 服务健康检查")
async def mineru_health():
    """检查 MinerU 子进程/容器服务状态"""
    try:
        from modules.mineru_tools.mineru_api_client import MinerUApiClient

        client = MinerUApiClient()
        health = client.health_check()
        return {"status": "ok", "detail": health}
    except Exception as e:
        return {"status": "unavailable", "detail": str(e)}