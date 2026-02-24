"""Export tailored resume and cover letter LaTeX files to PDF."""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger("agent.pdf_export")


class PDFExporter:
    """Render Jinja2 LaTeX templates and compile to PDF with pdflatex."""

    def __init__(self, config: Dict[str, Any]):
        self.cfg = config
        self.resume_template = Path(
            config.get("resume_template_path", "templates/resume_ats.tex.j2")
        )
        self.cover_template = Path(
            config.get("cover_letter_template_path", "templates/cover_letter.tex.j2")
        )
        self._verify_pdflatex()

    @staticmethod
    def _verify_pdflatex():
        """Check that pdflatex is available on the system."""
        if shutil.which("pdflatex") is None:
            logger.warning(
                "pdflatex not found! Install TeX Live: "
                "sudo apt install texlive-full  OR  "
                "sudo apt install texlive-latex-recommended "
                "texlive-fonts-recommended texlive-latex-extra "
                "texlive-fonts-extra"
            )

    # ──────────────────────────────────────────────
    def export(self, resume_data: Dict, cover_letter_data: Dict,
               output_dir: Path) -> Tuple[Path, Path]:
        """
        Generate resume.pdf and cover_letter.pdf in output_dir.
        Returns (resume_path, cover_path).
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        resume_tex = self._render_template(self.resume_template, resume_data)
        cover_tex = self._render_template(self.cover_template, cover_letter_data)

        resume_pdf = output_dir / "resume.pdf"
        cover_pdf = output_dir / "cover_letter.pdf"

        # Also save the .tex sources for debugging
        (output_dir / "resume.tex").write_text(resume_tex, encoding="utf-8")
        (output_dir / "cover_letter.tex").write_text(cover_tex, encoding="utf-8")

        self._compile_latex(resume_tex, resume_pdf)
        self._compile_latex(cover_tex, cover_pdf)

        logger.info("PDFs exported: %s, %s", resume_pdf, cover_pdf)
        return resume_pdf, cover_pdf

    # ──────────────────────────────────────────────
    @staticmethod
    def _sanitize_for_latex(text: str) -> str:
        """Remove characters that pdflatex cannot handle.
        
        - Strips emoji and chars outside BMP (U+10000+)
        - Keeps Swedish characters: ä å ö é Ä Å Ö É ü Ü
        - Strips other problematic Unicode (zero-width, control chars)
        """
        import re as _re
        # Remove characters outside BMP (emoji, etc.)
        # Swedish chars (ä=U+00E4, å=U+00E5, ö=U+00F6, é=U+00E9) are
        # well within BMP and are NOT affected by this filter
        text = _re.sub(r'[\U00010000-\U0010FFFF]', '', text)
        # Remove zero-width chars and other invisibles
        text = _re.sub(r'[\u200b\u200c\u200d\u2060\ufeff]', '', text)
        # Remove control chars except newline/tab
        text = _re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
        return text

    @staticmethod
    def _render_template(template_path: Path, data: Dict) -> str:
        """Render a Jinja2 LaTeX template with the given data."""
        import re as _re
        template_dir = str(template_path.parent)
        template_name = template_path.name

        # Sanitize all string values in data dict (strip emoji + control chars)
        def _sanitize_dict(d):
            if isinstance(d, dict):
                return {k: _sanitize_dict(v) for k, v in d.items()}
            elif isinstance(d, list):
                return [_sanitize_dict(v) for v in d]
            elif isinstance(d, str):
                # Strip emoji (outside BMP) — keeps ä å ö é intact
                s = _re.sub(r'[\U00010000-\U0010FFFF]', '', d)
                # Strip zero-width and control chars
                s = _re.sub(r'[\u200b\u200c\u200d\u2060\ufeff]', '', s)
                s = _re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', s)
                return s
            return d

        data = _sanitize_dict(data)

        # Use << >> delimiters to avoid conflict with LaTeX { }
        env = Environment(
            loader=FileSystemLoader(template_dir),
            block_start_string="<%",
            block_end_string="%>",
            variable_start_string="<<",
            variable_end_string=">>",
            comment_start_string="<#",
            comment_end_string="#>",
            autoescape=False,
        )
        tpl = env.get_template(template_name)
        return tpl.render(**data)

    # ──────────────────────────────────────────────
    @staticmethod
    def _compile_latex(tex_content: str, output_pdf: Path):
        """Compile LaTeX source to PDF using pdflatex."""
        if shutil.which("pdflatex") is None:
            logger.error(
                "pdflatex not found – cannot compile LaTeX to PDF. "
                "Install texlive: sudo apt install texlive-full"
            )
            # Write the .tex as fallback so user can compile manually
            fallback = output_pdf.with_suffix(".tex")
            fallback.write_text(tex_content, encoding="utf-8")
            logger.info("Saved .tex fallback: %s", fallback)
            return

        with tempfile.TemporaryDirectory(prefix="latex_") as tmpdir:
            tex_file = Path(tmpdir) / "document.tex"
            tex_file.write_text(tex_content, encoding="utf-8")

            # Run pdflatex twice (for references/layout convergence)
            for pass_num in (1, 2):
                result = subprocess.run(
                    [
                        "pdflatex",
                        "-interaction=nonstopmode",
                        "-halt-on-error",
                        "-output-directory", tmpdir,
                        str(tex_file),
                    ],
                    capture_output=True,
                    timeout=60,
                    cwd=tmpdir,
                    encoding="utf-8",
                    errors="replace",   # Handle non-UTF8 pdflatex output
                )
                if result.returncode != 0 and pass_num == 2:
                    # Log the error but don't crash the agent
                    log_file = Path(tmpdir) / "document.log"
                    log_content = ""
                    if log_file.exists():
                        log_content = log_file.read_text(
                            encoding="utf-8", errors="replace"
                        )[-2000:]
                    logger.error(
                        "pdflatex failed (pass %d):\nSTDOUT: %s\nLOG: %s",
                        pass_num,
                        result.stdout[-1000:] if result.stdout else "",
                        log_content,
                    )
                    # Save .tex as fallback
                    fallback = output_pdf.with_suffix(".tex")
                    fallback.write_text(tex_content, encoding="utf-8")
                    logger.info("Saved .tex fallback: %s", fallback)
                    return

            # Copy the PDF to the target location
            compiled = Path(tmpdir) / "document.pdf"
            if compiled.exists():
                shutil.copy2(str(compiled), str(output_pdf))
                logger.debug("LaTeX PDF compiled: %s", output_pdf)
            else:
                logger.error("pdflatex ran but no PDF produced")
                fallback = output_pdf.with_suffix(".tex")
                fallback.write_text(tex_content, encoding="utf-8")