;;; WINDOW_COUNT.LSP
;;; Command: WCNT
;;; AutoCAD / ZWCAD compatible (Visual LISP)
;;; Counts window/door marks in a selected visible area and makes a TABLE.
;;;
;;; Recognition priority
;;;  TEXT/MTEXT/ATTRIB inside a visible circle only.
;;;  Split AW + 01 and STG + 1A forms are supported.
;;; Nested blocks are walked recursively. Locked layers are readable.
;;; Off/frozen layers and invisible entities/attributes are ignored.

(vl-load-com)

(setq *wc-prefixes* nil) ; e.g. '("AW" "PW" "SSD"). nil = any letters + digits

;; Divider-search option lives only in the current CAD session.
;; It is intentionally not stored with setenv.
(if (not (boundp '*wc-divider-initialized*))
  (setq *wc-divider-initialized* nil *wc-check-divider* nil))

;; Remember the last output method during the current CAD session.
(if (not (boundp '*wc-output-mode*))
  (setq *wc-output-mode* 1))

;; Count-check slash option lives only in the current CAD session.
(if (not (boundp '*wc-draw-count-check*))
  (setq *wc-draw-count-check* nil))

;; TABLE export ordering is remembered only for the current CAD session.
(if (not (boundp '*wc-table-order-initialized*))
  (setq *wc-table-order-initialized* nil *wc-table-order-mode* 1))

(defun wc:v+ (a b) (mapcar '+ a b))
(defun wc:v- (a b) (mapcar '- a b))
(defun wc:vmul (v s) (mapcar '(lambda (x) (* x s)) v))
(defun wc:len (v) (sqrt (apply '+ (mapcar '(lambda (x) (* x x)) v))))

(defun wc:mx-id ()
  '((1.0 0.0 0.0 0.0) (0.0 1.0 0.0 0.0)
    (0.0 0.0 1.0 0.0) (0.0 0.0 0.0 1.0)))

(defun wc:mx-mul (a b / r i j k s row)
  (setq i 0 r nil)
  (repeat 4
    (setq j 0 row nil)
    (repeat 4
      (setq k 0 s 0.0)
      (repeat 4
        (setq s (+ s (* (nth k (nth i a)) (nth j (nth k b)))))
        (setq k (1+ k)))
      (setq row (append row (list s)) j (1+ j)))
    (setq r (append r (list row)) i (1+ i))) r)

(defun wc:mx-pt (m p / x y z)
  (setq x (car p) y (cadr p) z (if (caddr p) (caddr p) 0.0))
  (list
    (+ (* (nth 0 (nth 0 m)) x) (* (nth 1 (nth 0 m)) y)
       (* (nth 2 (nth 0 m)) z) (nth 3 (nth 0 m)))
    (+ (* (nth 0 (nth 1 m)) x) (* (nth 1 (nth 1 m)) y)
       (* (nth 2 (nth 1 m)) z) (nth 3 (nth 1 m)))
    (+ (* (nth 0 (nth 2 m)) x) (* (nth 1 (nth 2 m)) y)
       (* (nth 2 (nth 2 m)) z) (nth 3 (nth 2 m)))))

(defun wc:insert-mx (ed / p sx sy sz a c s)
  (setq p (cdr (assoc 10 ed))
        sx (cond ((cdr (assoc 41 ed))) (1.0))
        sy (cond ((cdr (assoc 42 ed))) (1.0))
        sz (cond ((cdr (assoc 43 ed))) (1.0))
        a  (cond ((cdr (assoc 50 ed))) (0.0))
        c (cos a) s (sin a))
  (list
    (list (* sx c) (* sy (- s)) 0.0 (car p))
    (list (* sx s) (* sy c)     0.0 (cadr p))
    (list 0.0       0.0          sz  (if (caddr p) (caddr p) 0.0))
    '(0.0 0.0 0.0 1.0)))

(defun wc:layer-visible-p (lay / d)
  (and (setq d (tblsearch "LAYER" lay))
       (>= (cdr (assoc 62 d)) 0)
       (= 0 (logand 1 (cdr (assoc 70 d))))))

(defun wc:entity-visible-p (ed)
  (and (wc:layer-visible-p (cdr (assoc 8 ed)))
       (/= 1 (cdr (assoc 60 ed)))))

(defun wc:clean (s / out i ch n)
  (setq s (strcase (if s s "")) out "" i 1)
  ;; use part after last $ (BIND names)
  (while (setq n (vl-string-search "$" s))
    (setq s (substr s (+ n 2))))
  (while (<= i (strlen s))
    (setq ch (substr s i 1))
    (if (or (and (>= (ascii ch) 65) (<= (ascii ch) 90))
            (and (>= (ascii ch) 48) (<= (ascii ch) 57)))
      (setq out (strcat out ch)))
    (setq i (1+ i))) out)

(defun wc:mark-p (s / i letters nums tail ch)
  (setq s (wc:clean s) i 1 letters "" nums "" ok T)
  (while (and (<= i (strlen s))
              (setq ch (substr s i 1))
              (>= (ascii ch) 65) (<= (ascii ch) 90))
    (setq letters (strcat letters ch) i (1+ i)))
  (while (and (<= i (strlen s))
              (setq ch (substr s i 1))
              (>= (ascii ch) 48) (<= (ascii ch) 57))
    (setq nums (strcat nums ch) i (1+ i)))
  ;; Optional alphabetic suffix: STG1A, AW12B, etc.
  (setq tail "")
  (while (and (<= i (strlen s))
              (setq ch (substr s i 1))
              (>= (ascii ch) 65) (<= (ascii ch) 90))
    (setq tail (strcat tail ch) i (1+ i)))
  (and (> (strlen letters) 0) (> (strlen nums) 0)
       (> (strlen s) 1) (= i (1+ (strlen s)))
       (or (null *wc-prefixes*) (member letters *wc-prefixes*))))

(defun wc:mark-prefix (s / i ch r)
  (setq s (wc:clean s) i 1 r "")
  (while (and (<= i (strlen s))
              (setq ch (substr s i 1))
              (>= (ascii ch) 65) (<= (ascii ch) 90))
    (setq r (strcat r ch) i (1+ i))) r)

(defun wc:normalize-mark (s / i ch pre num tail phase)
  ;; PREFIX + at least two digit number + optional alphabetic suffix.
  ;; AW1 -> AW01, STG10 -> STG10, STG1A -> STG01A.
  (setq s (wc:clean s) i 1 pre "" num "" tail "" phase 0)
  (while (<= i (strlen s))
    (setq ch (substr s i 1))
    (cond
      ((and (= phase 0) (>= (ascii ch) 65) (<= (ascii ch) 90))
       (setq pre (strcat pre ch)))
      ((and (<= phase 1) (>= (ascii ch) 48) (<= (ascii ch) 57))
       (setq phase 1 num (strcat num ch)))
      (T (setq phase 2 tail (strcat tail ch))))
    (setq i (1+ i)))
  (if (= (strlen num) 1) (setq num (strcat "0" num)))
  (strcat pre num tail))

(defun wc:known-mark-p (s / p)
  ;; Outside a circle, accept only prefixes proven by a circle in this selection.
  ;; This prevents room names (B101) and generic block names (INSUL1).
  (and (wc:mark-p s)
       (setq p (wc:mark-prefix s))
       (member p *wc-seen-prefixes*)))

(defun wc:letters (s / r i ch)
  (setq s (wc:clean s) r "" i 1)
  (while (<= i (strlen s))
    (setq ch (substr s i 1))
    (if (and (>= (ascii ch) 65) (<= (ascii ch) 90))
      (setq r (strcat r ch))) (setq i (1+ i))) r)

(defun wc:digits (s / r i ch)
  (setq s (wc:clean s) r "" i 1)
  (while (<= i (strlen s))
    (setq ch (substr s i 1))
    (if (and (>= (ascii ch) 48) (<= (ascii ch) 57))
      (setq r (strcat r ch))) (setq i (1+ i))) r)

(defun wc:numtail (s / i ch n a phase bad)
  ;; Returns digit+optional-letter fragment (1, 01, 1A); otherwise "".
  (setq s (wc:clean s) i 1 n "" a "" phase 0 bad nil)
  (while (and (<= i (strlen s)) (not bad))
    (setq ch (substr s i 1))
    (cond
      ((and (= phase 0) (>= (ascii ch) 48) (<= (ascii ch) 57))
       (setq n (strcat n ch)))
      ((and (> (strlen n) 0) (>= (ascii ch) 65) (<= (ascii ch) 90))
       (setq phase 1 a (strcat a ch)))
      (T (setq bad T)))
    (setq i (1+ i)))
  (if (and (not bad) (> (strlen n) 0)) (strcat n a) ""))

(defun wc:text-center (e m / o mn mx er p1 p2)
  (setq o (vlax-ename->vla-object e)
        er (vl-catch-all-apply 'vla-getboundingbox (list o 'mn 'mx)))
  (if (vl-catch-all-error-p er)
    (wc:mx-pt m (cdr (assoc 10 (entget e))))
    (progn
      (setq p1 (vlax-safearray->list mn) p2 (vlax-safearray->list mx))
      ;; Transform the text bounding-box centre with its parent block matrix.
      ;; Using the insertion point caused nearby UP/DN notes to enter a circle.
      (if (equal m (wc:mx-id) 1e-12)
        (wc:vmul (wc:v+ p1 p2) 0.5)
        (wc:mx-pt m (wc:vmul (wc:v+ p1 p2) 0.5))))))

(defun wc:add-token (txt pt src)
  (if (> (strlen (wc:clean txt)) 0)
    (setq *wc-tokens* (cons (list (wc:clean txt) pt src) *wc-tokens*))))

(defun wc:add-circle (pt rad src)
  (if (> rad 0.0)
    (setq *wc-circles* (cons (list pt rad src) *wc-circles*))))

(defun wc:add-line (p1 p2 src)
  (if (> (wc:len (wc:v- p2 p1)) 1e-8)
    (setq *wc-lines* (cons (list p1 p2 src) *wc-lines*))))

(defun wc:block-name (ed / n o)
  (setq n (cdr (assoc 2 ed)))
  (if (and n (= (substr n 1 2) "*U"))
    (progn
      (setq o (vlax-ename->vla-object (cdr (assoc -1 ed))))
      (if (vlax-property-available-p o 'EffectiveName)
        (setq n (vla-get-EffectiveName o))))) n)

(defun wc:walk-insert (e pm stack / ed name bm be cm nxt ad s p)
  (setq ed (entget e) name (wc:block-name ed))
  (if (and name (not (member (strcase name) stack)))
    (progn
      (setq cm (wc:mx-mul pm (wc:insert-mx ed)))
      ;; attributes attached to this reference
      (setq nxt (entnext e))
      (while (and nxt (= "ATTRIB" (cdr (assoc 0 (setq ad (entget nxt))))))
        (if (wc:entity-visible-p ad)
          (wc:add-token (cdr (assoc 1 ad)) (wc:mx-pt pm (cdr (assoc 10 ad))) e))
        (setq nxt (entnext nxt)))
      ;; block name as a fallback candidate at insertion point
      (if (wc:mark-p name)
        (setq *wc-blockmarks*
          (cons (list (wc:clean name) (wc:mx-pt pm (cdr (assoc 10 ed))) e)
                *wc-blockmarks*)))
      ;; walk definition (xref definitions are skipped; bound blocks are normal)
      (if (and (setq bm (tblobjname "BLOCK" name))
               (= 0 (logand 4 (cdr (assoc 70 (entget bm))))))
        (progn
          (setq be (entnext bm))
          (while (and be (/= "ENDBLK" (cdr (assoc 0 (entget be)))))
            (wc:walk be cm (cons (strcase name) stack))
            (setq be (entnext be))))))))

(defun wc:walk (e m stack / ed typ p r sx sy)
  (setq ed (entget e) typ (cdr (assoc 0 ed)))
  (if (wc:entity-visible-p ed)
    (cond
      ((= typ "INSERT") (wc:walk-insert e m stack))
      ;; ATTDEF is not counted here: its actual INSERT reference is read as ATTRIB.
      ((member typ '("TEXT" "MTEXT" "ATTRIB"))
       (wc:add-token (cdr (assoc 1 ed)) (wc:text-center e m) e))
      ((= typ "CIRCLE")
       (setq p (wc:mx-pt m (cdr (assoc 10 ed)))
             sx (wc:len (wc:v- (wc:mx-pt m '(1.0 0.0 0.0))
                               (wc:mx-pt m '(0.0 0.0 0.0))))
             sy (wc:len (wc:v- (wc:mx-pt m '(0.0 1.0 0.0))
                               (wc:mx-pt m '(0.0 0.0 0.0))))
             r (* (cdr (assoc 40 ed)) (max sx sy)))
       (wc:add-circle p r e))
      ((and *wc-check-divider* (= typ "LINE"))
       (wc:add-line (wc:mx-pt m (cdr (assoc 10 ed)))
                    (wc:mx-pt m (cdr (assoc 11 ed))) e)))))

(defun wc:inside-p (pt cir)
  (<= (wc:len (wc:v- pt (car cir))) (+ (cadr cir) 1e-8)))

(defun wc:dot2 (a b) (+ (* (car a) (car b)) (* (cadr a) (cadr b))))

(defun wc:point-seg-dist (p a b / ab ap den u q)
  (setq ab (wc:v- b a) ap (wc:v- p a) den (wc:dot2 ab ab))
  (if (<= den 1e-12)
    (wc:len ap)
    (progn
      (setq u (/ (wc:dot2 ap ab) den))
      (cond ((< u 0.0) (setq u 0.0)) ((> u 1.0) (setq u 1.0)))
      (setq q (wc:v+ a (wc:vmul ab u)))
      (wc:len (wc:v- p q)))))

(defun wc:divider-p (cir / yes c r a b ln mid llen)
  ;; A tag divider passes near the centre and is roughly one diameter long.
  (setq yes nil c (car cir) r (cadr cir))
  (foreach ln *wc-lines*
    (setq a (car ln) b (cadr ln)
          llen (wc:len (wc:v- b a))
          mid (wc:vmul (wc:v+ a b) 0.5))
    (if (and (>= llen (* 1.45 r))
             (<= llen (* 2.30 r))
             (<= (wc:point-seg-dist c a b) (* 0.18 r))
             (<= (wc:len (wc:v- mid c)) (* 0.30 r))
             (<= (wc:len (wc:v- a c)) (* 1.20 r))
             (<= (wc:len (wc:v- b c)) (* 1.20 r)))
      (setq yes T))) yes)

(defun wc:median-radius (circles / rs n)
  (setq rs (vl-sort (mapcar 'cadr circles) '<) n (length rs))
  (if (> n 0) (nth (fix (/ n 2)) rs) 1.0))

(defun wc:filter-divider-lines (lines circles / r lo hi)
  ;; Most architectural lines are rejected once, before circle comparisons.
  (setq r (wc:median-radius circles) lo (* 1.20 r) hi (* 2.60 r))
  (vl-remove-if-not
    '(lambda (ln)
       (and (>= (wc:len (wc:v- (cadr ln) (car ln))) lo)
            (<= (wc:len (wc:v- (cadr ln) (car ln))) hi)))
    lines))

(defun wc:circle-mark (cir / ts full pre num s frag d bestf bestp bestn)
  (setq ts (vl-remove-if-not '(lambda (x) (wc:inside-p (cadr x) cir)) *wc-tokens*)
        full nil pre "" num "" bestf nil bestp nil bestn nil)
  (foreach x ts
    (setq s (car x) d (wc:len (wc:v- (cadr x) (car cir))))
    (cond ((wc:mark-p s)
           (if (or (null bestf) (< d bestf))
             (setq full s bestf d)))
          ((and (> (strlen (wc:letters s)) 0)
                (= (strlen (wc:digits s)) 0))
           (if (or (null bestp) (< d bestp))
             (setq pre (wc:letters s) bestp d)))
          ((> (strlen (setq frag (wc:numtail s))) 0)
           (if (or (null bestn) (< d bestn))
             (setq num frag bestn d)))))
  (cond (full (wc:normalize-mark full))
        ((wc:mark-p (strcat pre num)) (wc:normalize-mark (strcat pre num)))
        (nil)))

(defun wc:add-count (key / a)
  (if (setq a (assoc key *wc-counts*))
    (setq *wc-counts* (subst (cons key (1+ (cdr a))) a *wc-counts*))
    (setq *wc-counts* (cons (cons key 1) *wc-counts*))))

(defun wc:total-count (rows / total)
  (setq total 0)
  (foreach pair rows
    (setq total (+ total (cdr pair))))
  total)

(defun wc:draw-count-check (cir / c r d p1 p2)
  ;; Draw one NW-SE slash, approximately the diameter of the window tag.
  (setq c (car cir)
        r (cadr cir)
        d (* 0.85 r)
        p1 (wc:v+ c (list (- d) d 0.0))
        p2 (wc:v+ c (list d (- d) 0.0)))
  (entmakex
    (list '(0 . "LINE")
          '(100 . "AcDbEntity")
          (cons 8 (getvar "CLAYER"))
          '(100 . "AcDbLine")
          (cons 10 p1)
          (cons 11 p2))))

(defun wc:draw-count-checks (/ ss e)
  ;; Keep only the newly drawn count-check slashes selected when WCNT ends.
  (setq ss (ssadd))
  (foreach cir *wc-used-circles*
    (if (setq e (wc:draw-count-check cir))
      (ssadd e ss)))
  (if (> (sslength ss) 0)
    (sssetfirst nil ss))
  ss)

(defun wc:ask-count-check-option (/ choice cur)
  (setq cur (if *wc-draw-count-check* 2 1))
  (initget 6)
  (setq choice
    (getint
      (strcat "\n수량체크 사선 표시 [1 OFF / 2 ON] <"
              (itoa cur) ">: ")))
  (if (null choice) (setq choice cur))
  (if (not (member choice '(1 2))) (setq choice cur))
  (setq *wc-draw-count-check* (= choice 2))
  (princ
    (strcat "\n수량체크 사선 표시: "
            (if *wc-draw-count-check* "ON" "OFF"))))

(defun wc:near-used-circle-p (pt / yes)
  (setq yes nil)
  (foreach c *wc-used-circles*
    (if (<= (wc:len (wc:v- pt (car c))) (* 1.25 (cadr c))) (setq yes T))) yes)

(defun wc:near-complete-token-p (pt / yes tol)
  (setq yes nil tol (max 1e-6 (* 5.0 (getvar "TEXTSIZE"))))
  (foreach x *wc-tokens*
    (if (and (wc:mark-p (car x))
             (<= (wc:len (wc:v- pt (cadr x))) tol))
      (setq yes T))) yes)

(defun wc:make-table (pt rows th / doc sp tb rh cw1 cw2 i pair)
  (setq doc (vla-get-ActiveDocument (vlax-get-acad-object))
        sp (if (= 1 (getvar "CVPORT"))
             (vla-get-PaperSpace doc) (vla-get-ModelSpace doc))
        rh (* 2.0 th)
        cw1 (* 8.0 th)
        cw2 (* 5.0 th))
  (setq tb (vla-AddTable sp (vlax-3d-point pt) (+ 3 (length rows)) 2 rh cw1))
  ;; Row types: 1=title, 2=header, 4=data
  (vla-SetTextHeight tb 1 (* 1.2 th))
  (vla-SetTextHeight tb 2 th)
  (vla-SetTextHeight tb 4 th)
  (vla-SetColumnWidth tb 0 cw1)
  (vla-SetColumnWidth tb 1 cw2)
  (vla-SetText tb 0 0 "창호 부호별 수량")
  (vla-SetText tb 1 0 "창호부호") (vla-SetText tb 1 1 "개수")
  (setq i 2)
  (foreach pair rows
    (vla-SetText tb i 0 (car pair))
    (vla-SetText tb i 1 (itoa (cdr pair)))
    (setq i (1+ i)))
  (vla-SetText tb i 0 "총계")
  (vla-SetText tb i 1 (itoa (wc:total-count rows)))
  tb)

(defun wc:sort-pairs (a b) (< (car a) (car b)))

(defun wc:rows-to-tsv (rows / s)
  ;; No heading row: MARK<TAB>COUNT, ready to paste into Excel.
  ;; Use LF only. Windows text-file output converts it to CRLF once.
  (setq s "")
  (foreach pair rows
    (setq s (strcat s (car pair) "\t" (itoa (cdr pair)) "\n")))
  (strcat s "총계\t" (itoa (wc:total-count rows)) "\n"))

(defun wc:copy-text (s / fn f sh cmd er rc)
  ;; clip.exe is synchronous here, so success means the clipboard was updated.
  (setq fn (vl-filename-mktemp "wcnt_clip_" nil ".txt"))
  (if (and fn (setq f (open fn "w")))
    (progn
      (princ s f)
      (close f)
      (setq sh (vl-catch-all-apply 'vlax-create-object (list "WScript.Shell")))
      (if (vl-catch-all-error-p sh)
        (progn (vl-file-delete fn) nil)
        (progn
          (setq cmd (strcat "cmd.exe /d /c type \"" fn "\" | clip.exe")
                er (vl-catch-all-apply
                     'vlax-invoke-method
                     (list sh 'Run cmd 0 :vlax-true)))
          (if (not (vl-catch-all-error-p er)) (setq rc er))
          (vl-catch-all-apply 'vlax-release-object (list sh))
          (vl-file-delete fn)
          (and (numberp rc) (= rc 0)))))
    nil))

(defun wc:ask-copy (rows / ans)
  ;; Enter returns an empty string. Esc is caught and means 'do not copy'.
  (setq ans
    (vl-catch-all-apply
      'getstring
      (list "\n엑셀용 수량을 클립보드에 복사 <Enter=복사 / Esc=안 함>: ")))
  (if (vl-catch-all-error-p ans)
    (princ "\n클립보드 복사를 건너뛰었습니다.")
    (if (wc:copy-text (wc:rows-to-tsv rows))
      (princ "\n클립보드에 복사했습니다. 엑셀에서 붙여넣으세요.")
      (princ "\n클립보드 복사에 실패했습니다."))))

(defun wc:copy-result (rows)
  (if (wc:copy-text (wc:rows-to-tsv rows))
    (princ "\n클립보드에 복사했습니다. 엑셀에서 붙여넣으세요.")
    (princ "\n클립보드 복사에 실패했습니다.")))

(defun wc:table-order-p (a b / pa pb)
  ;; Row-major drawing order by each TABLE's upper-left insertion point:
  ;; top to bottom, and left to right within the same horizontal row.
  (setq pa (cdr (assoc 10 (entget a)))
        pb (cdr (assoc 10 (entget b))))
  (if (equal (cadr pa) (cadr pb)
             (max 1e-8 (* 0.5 (getvar "TEXTSIZE"))))
    (< (car pa) (car pb))
    (> (cadr pa) (cadr pb))))

(defun wc:ask-table-order (/ choice)
  (initget 6)
  (setq choice
    (getint
      (strcat "\nTABLE 정렬 [1 선택순 / 2 좌상단 위치순(좌→우, 위→아래)] <"
              (itoa *wc-table-order-mode*) ">: ")))
  (if (null choice) (setq choice *wc-table-order-mode*))
  (if (not (member choice '(1 2))) (setq choice *wc-table-order-mode*))
  (setq *wc-table-order-mode* choice
        *wc-table-order-initialized* T)
  (princ
    (strcat "\nTABLE 정렬: "
            (if (= choice 1) "선택순" "좌상단 위치순"))))

(defun wc:ss-to-list (ss / i out)
  (setq i 0 out nil)
  (repeat (sslength ss)
    (setq out (cons (ssname ss i) out)
          i (1+ i)))
  (reverse out))

(defun wc:table-data (e / o nr nc r c row rows val)
  (setq o (vlax-ename->vla-object e)
        nr (vla-get-Rows o)
        nc (vla-get-Columns o)
        r 0 rows nil)
  (repeat nr
    (setq c 0 row nil)
    (repeat nc
      (setq val (vl-catch-all-apply 'vla-GetText (list o r c)))
      (if (vl-catch-all-error-p val) (setq val ""))
      (setq row (append row (list val))
            c (1+ c)))
    (setq rows (append rows (list row))
          r (1+ r)))
  (list nc rows))

(defun wc:blank-cells (n / out)
  (setq out nil)
  (repeat n (setq out (cons "" out)))
  out)

(defun wc:join-tabs (cells / out first)
  (setq out "" first T)
  (foreach cell cells
    (setq out (strcat out (if first "" "\t") (if cell cell ""))
          first nil))
  out)

(defun wc:tables-to-tsv (ents / tables maxrows td cols rows r fields row out)
  ;; Put selected tables directly side by side with no empty Excel column.
  (setq tables nil maxrows 0)
  (foreach e ents
    (setq td (wc:table-data e)
          tables (append tables (list td)))
    (if (> (length (cadr td)) maxrows)
      (setq maxrows (length (cadr td)))))
  (setq r 0 out "")
  (repeat maxrows
    (setq fields nil)
    (foreach td tables
      (setq cols (car td) rows (cadr td) row (nth r rows))
      (setq fields (append fields (if row row (wc:blank-cells cols)))))
    (setq out (strcat out (wc:join-tabs fields) "\n")
          r (1+ r)))
  out)

(defun wc:table-counts (e / td rows counts row mark qty old)
  (setq td (wc:table-data e) rows (cadr td) counts nil)
  (foreach row rows
    (if (and (>= (length row) 2) (wc:mark-p (car row)))
      (progn
        (setq mark (wc:normalize-mark (car row))
              qty (atoi (cadr row)))
        (if (setq old (assoc mark counts))
          (setq counts (subst (cons mark (+ (cdr old) qty)) old counts))
          (setq counts (cons (cons mark qty) counts))))))
  counts)

(defun wc:summary-data (ents / tablecounts keys counts pair)
  (setq tablecounts nil keys nil)
  (foreach e ents
    (setq counts (wc:table-counts e)
          tablecounts (append tablecounts (list counts)))
    (foreach pair counts
      (if (not (member (car pair) keys))
        (setq keys (cons (car pair) keys)))))
  (list (vl-sort keys '<) tablecounts))

(defun wc:counts-total (counts / total pair)
  (setq total 0)
  (foreach pair counts (setq total (+ total (cdr pair))))
  total)

(defun wc:summary-to-tsv (ents / sd keys tables fields i out key counts
                                pair qty rowsum grand)
  (setq sd (wc:summary-data ents)
        keys (car sd) tables (cadr sd)
        fields (list "창호부호") i 1)
  (repeat (length tables)
    (setq fields (append fields (list (strcat "표" (itoa i))))
          i (1+ i)))
  (setq out (strcat (wc:join-tabs
                      (append fields (list "부호별 합계"))) "\n")
        grand 0)
  (foreach key keys
    (setq fields (list key) rowsum 0)
    (foreach counts tables
      (setq qty (if (setq pair (assoc key counts)) (cdr pair) 0)
            rowsum (+ rowsum qty)
            fields (append fields (list (itoa qty)))))
    (setq out (strcat out
                      (wc:join-tabs (append fields (list (itoa rowsum))))
                      "\n")))
  (setq fields (list "표별 합계"))
  (foreach counts tables
    (setq qty (wc:counts-total counts)
          grand (+ grand qty)
          fields (append fields (list (itoa qty)))))
  (strcat out (wc:join-tabs (append fields (list (itoa grand)))) "\n"))

(defun wc:make-summary-table (pt ents th / doc sp sd keys tables nr nc tb
                                     rh i r key counts pair qty rowsum grand)
  (setq doc (vla-get-ActiveDocument (vlax-get-acad-object))
        sp (if (= 1 (getvar "CVPORT"))
             (vla-get-PaperSpace doc) (vla-get-ModelSpace doc))
        sd (wc:summary-data ents)
        keys (car sd) tables (cadr sd)
        nr (+ 3 (length keys))
        nc (+ 2 (length tables))
        rh (* 2.0 th)
        tb (vla-AddTable sp (vlax-3d-point pt) nr nc rh (* 5.0 th)))
  (vla-MergeCells tb 0 0 0 (1- nc))
  (vla-SetTextHeight tb 1 (* 1.2 th))
  (vla-SetTextHeight tb 2 th)
  (vla-SetTextHeight tb 4 th)
  (vla-SetColumnWidth tb 0 (* 7.0 th))
  (vla-SetText tb 0 0 "창호 부호별·표별 집계")
  (vla-SetText tb 1 0 "창호부호")
  (setq i 0)
  (foreach counts tables
    (vla-SetText tb 1 (1+ i) (strcat "표" (itoa (1+ i))))
    (setq i (1+ i)))
  (vla-SetText tb 1 (1- nc) "부호별 합계")
  (setq r 2)
  (foreach key keys
    (vla-SetText tb r 0 key)
    (setq i 0 rowsum 0)
    (foreach counts tables
      (setq qty (if (setq pair (assoc key counts)) (cdr pair) 0)
            rowsum (+ rowsum qty))
      (vla-SetText tb r (1+ i) (itoa qty))
      (setq i (1+ i)))
    (vla-SetText tb r (1- nc) (itoa rowsum))
    (setq r (1+ r)))
  (vla-SetText tb r 0 "표별 합계")
  (setq i 0 grand 0)
  (foreach counts tables
    (setq qty (wc:counts-total counts)
          grand (+ grand qty))
    (vla-SetText tb r (1+ i) (itoa qty))
    (setq i (1+ i)))
  (vla-SetText tb r (1- nc) (itoa grand))
  tb)

(defun wc:clear-active-selection (/ doc aset)
  (sssetfirst nil nil)
  (setq doc (vla-get-ActiveDocument (vlax-get-acad-object))
        aset (vl-catch-all-apply 'vla-get-ActiveSelectionSet (list doc)))
  (if (not (vl-catch-all-error-p aset))
    (vl-catch-all-apply 'vla-Clear (list aset)))
  nil)

(defun wc:copy-selected-tables (ss / ents text choice applymode th pt)
  (setq ents (wc:ss-to-list ss))
  (if (= *wc-table-order-mode* 2)
    (setq ents (vl-sort ents 'wc:table-order-p)))
  (setq
        text (wc:tables-to-tsv ents))
  (if (wc:copy-text text)
    (princ
      (strcat "\n선택한 표 " (itoa (length ents))
              "개를 "
              (if (= *wc-table-order-mode* 1) "선택순" "좌상단 위치순")
              "으로 클립보드에 복사했습니다."))
    (princ "\n선택한 표의 클립보드 복사에 실패했습니다."))
  ;; The original side-by-side tables are already on the clipboard here.
  (initget 6)
  (setq choice
    (getint "\n집계표를 작성할까요? [1 아니요 / 2 네] <1>: "))
  (if (null choice) (setq choice 1))
  (if (not (member choice '(1 2))) (setq choice 1))
  (if (= choice 2)
    (progn
      (initget 6)
      (setq applymode
        (getint "\n집계표를 어디에 적용할까요? [1 클립보드 / 2 CAD 내 집계표] <1>: "))
      (if (null applymode) (setq applymode 1))
      (if (not (member applymode '(1 2))) (setq applymode 1))
      (cond
        ((= applymode 2)
         (setq th (wc:saved-height))
         (if (null th) (setq th (wc:ask-save-height)))
         (if (setq pt (getpoint "\n집계표를 작성할 위치 지정: "))
           (wc:make-summary-table pt ents th)))
        (T
         (if (wc:copy-text (wc:summary-to-tsv ents))
           (princ "\n집계표를 클립보드에 복사했습니다.")
           (princ "\n집계표 클립보드 복사에 실패했습니다."))))))
  ;; Clear both the visual and COM active selection in ZWCAD.
  (wc:clear-active-selection)
  (princ))

(defun wc:saved-height (/ s h)
  (setq s (getenv "WCNT_TABLE_TEXT_HEIGHT"))
  (if (and s (> (setq h (atof s)) 0.0)) h nil))

(defun wc:ask-save-height (/ h defth)
  (setq defth (getvar "TEXTSIZE"))
  (if (<= defth 0.0) (setq defth 2.5))
  (initget 6)
  (setq h (getdist (strcat "\n표 글자높이 <" (rtos defth 2 2) ">: ")))
  (if (null h) (setq h defth))
  (setenv "WCNT_TABLE_TEXT_HEIGHT" (rtos h 2 8))
  h)

(defun wc:ask-divider-option (first / choice cur)
  (setq cur (if *wc-check-divider* 2 1))
  (initget 6)
  (setq choice
    (getint
      (strcat
        (if first "\n최초 검색옵션" "\n검색옵션 변경")
        " [1 가로선 검사 OFF-빠름 / 2 ON-정확도 향상] <"
        (itoa cur) ">: ")))
  (if (null choice) (setq choice cur))
  (if (not (member choice '(1 2))) (setq choice cur))
  (setq *wc-check-divider* (= choice 2)
        *wc-divider-initialized* T)
  (princ
    (strcat "\n가로선 검사: "
      (if *wc-check-divider* "ON (정확도 향상/속도 저하)" "OFF (빠른 모드)"))))


(defun wc:show-help ()
  (princ "\n")
  (princ "\n================ WCNT / WCNTT 도움말 ================")
  (princ "\n[WCNT]")
  (princ "\n  - 범위 안 창호부호를 자동 집계합니다.")
  (princ "\n  - 블록 및 중첩블록 내부 문자도 검색합니다.")
  (princ "\n  - 꺼짐/동결 레이어와 숨김 객체는 집계에서 제외됩니다.")
  (princ "\n  - AW1처럼 숫자가 한 자리면 AW01 형식으로 정리해서 집계합니다.")
  (princ "\n")
  (princ "\n[가로선 검사]")
  (princ "\n  - Enter로 ON/OFF 변경")
  (princ "\n  - OFF : 빠르게 검색")
  (princ "\n  - ON  : 창호 원 내부의 가로선까지 확인하여 더 엄격하게 판별")
  (princ "\n")
  (princ "\n[출력방식]")
  (princ "\n  1 : 집계 결과를 클립보드로 복사")
  (princ "\n  2 : 도면에 집계표 작성")
  (princ "\n  3 : 집계표 작성 + 클립보드 복사")
  (princ "\n  4 : 집계표 문자크기 재설정")
  (princ "\n  5 : 수량체크 표시 여부 변경")
  (princ "\n")
  (princ "\n[수량체크 표시]")
  (princ "\n  - ON  : 실제 집계된 창호 원에 / 사선을 표시")
  (princ "\n  - OFF : 수량만 집계하고 체크 사선은 표시하지 않음")
  (princ "\n")
  (princ "\n[WCNTT]")
  (princ "\n  - WCNT로 만든 여러 집계표를 동시 선택하면 통합 집계표 작성")
  (princ "\n========================================================")
  (princ))

(defun wc:run (/ *error* olderr ss i e p1 p2 pt mark rows th mode pre mc
                        divstate ssfilter divchoice)
  (setq olderr *error*)
  (princ "\nWINDOW_COUNT v31")
  (defun *error* (msg)
    (if (and msg (not (wcmatch (strcase msg) "*CANCEL*,*QUIT*")))
      (princ (strcat "\n오류: " msg)))
    (setq *error* olderr) (princ))
  (setq *wc-tokens* nil *wc-circles* nil *wc-blockmarks* nil
        *wc-lines* nil *wc-counts* nil *wc-used-circles* nil
        *wc-seen-prefixes* nil)
  ;; Ask once after CAD starts; subsequent runs reuse the session value.
  (if (not *wc-divider-initialized*)
    (wc:ask-divider-option T))
  (setq divstate (if *wc-check-divider* "ON (정확도 향상/속도 저하)" "OFF (빠른 모드)")
        ssfilter (if *wc-check-divider*
                   '((0 . "INSERT,TEXT,MTEXT,CIRCLE,LINE"))
                   '((0 . "INSERT,TEXT,MTEXT,CIRCLE"))))
  (princ "\n집계할 평면도 영역의 두 모서리를 지정하세요. 첫 점 지정 전 ?를 입력하면 상세 사용법을 표시합니다.")
  (setq p1 "START")
  (while (= (type p1) 'STR)
    ;; Bit 128 lets GETPOINT accept ? as a free-form string while mouse point
    ;; selection and Enter continue to work normally.
    (initget 128)
    (setq p1
      (getpoint
        (strcat "\n첫 번째 모서리 지정 [가로선 검사 " divstate
                "] [?=도움말 / Enter=검색옵션 변경]: ")))
    (cond
      ((= p1 "?") (wc:show-help))
      ((= (type p1) 'STR)
       (princ "\n알 수 없는 입력입니다. 도움말은 ? 를 입력하세요."))))
  (if (and p1
           (setq p2 (getcorner p1 "\n반대 모서리: "))
           (setq ss (ssget "_C" p1 p2 ssfilter)))
    (progn
      (setq i 0)
      (repeat (sslength ss)
        (setq e (ssname ss i)) (wc:walk e (wc:mx-id) nil) (setq i (1+ i)))
      ;; First keep only circles that already contain a plausible mark.
      (setq *wc-mark-circles* nil)
      (foreach c *wc-circles*
        (if (setq mark (wc:circle-mark c))
          (setq *wc-mark-circles* (cons (list c mark) *wc-mark-circles*))))
      (if *wc-check-divider*
        (setq *wc-lines*
          (wc:filter-divider-lines *wc-lines* (mapcar 'car *wc-mark-circles*))))
      (foreach mc *wc-mark-circles*
        (setq c (car mc) mark (cadr mc))
        (if (or (not *wc-check-divider*) (wc:divider-p c))
          (progn
            (wc:add-count mark)
            (setq pre (wc:mark-prefix mark))
            (if (and (> (strlen pre) 0) (not (member pre *wc-seen-prefixes*)))
              (setq *wc-seen-prefixes* (cons pre *wc-seen-prefixes*)))
            (setq *wc-used-circles* (cons c *wc-used-circles*)))))
      (setq rows (vl-sort *wc-counts* 'wc:sort-pairs))
      (if rows
        (progn
          (initget 6)
          (setq mode (getint
            (strcat "\n출력 방식 [1 클립보드 / 2 표 작성 / 3 표+클립보드"
                    " / 4 문자크기 재설정 / 5 수량체크여부] <"
                    (itoa *wc-output-mode*) ">: ")))
          ;; Space or Enter without input reuses the previous output method.
          (if (null mode) (setq mode *wc-output-mode*))
          (if (not (member mode '(1 2 3 4 5))) (setq mode *wc-output-mode*))
          (if (member mode '(1 2 3)) (setq *wc-output-mode* mode))
          ;; Option 5 changes only the slash setting, then continues with the
          ;; previously used clipboard/table output method in this run.
          (if (= mode 5)
            (progn
              (wc:ask-count-check-option)
              (setq mode *wc-output-mode*)))
          (cond
            ((= mode 4)
             (setq th (wc:ask-save-height))
             (princ (strcat "\n표 글자높이를 " (rtos th 2 2) "로 저장했습니다.")))
            (T
             (if (member mode '(2 3))
               (progn
                 (setq th (wc:saved-height))
                 (if (null th) (setq th (wc:ask-save-height)))
                 (if (setq pt (getpoint
                   (strcat "\n표를 작성할 위치점 지정 [문자높이 " (rtos th 2 2) "]: ")))
                   (wc:make-table pt rows th))))
             (if (member mode '(1 3)) (wc:copy-result rows))
             (if *wc-draw-count-check* (wc:draw-count-checks))
             (princ
               (strcat "\n완료: 총 " (itoa (wc:total-count rows))
                       "개를 집계했습니다. (부호 종류 "
                       (itoa (length rows)) "개)")))))
        (princ "\n인식된 창호부호가 없습니다.")))
    (progn
      (wc:ask-divider-option nil)
      (princ "\n변경된 검색옵션으로 범위 선택을 다시 시작합니다.")
      (wc:run)))
  (setq *error* olderr) (princ))

(defun wc:table-entity-p (e / ed obj name)
  ;; Explicit checks are more reliable than an ssget TABLE filter in ZWCAD.
  (setq ed (entget e))
  (or (= (cdr (assoc 0 ed)) "ACAD_TABLE")
      (and
        (setq obj (vl-catch-all-apply 'vlax-ename->vla-object (list e)))
        (not (vl-catch-all-error-p obj))
        (setq name (vl-catch-all-apply 'vla-get-ObjectName (list obj)))
        (not (vl-catch-all-error-p name))
        (= (strcase name) "ACDBTABLE"))))

(defun wc:preselected-tables (/ doc aset obj raw ss i e)
  ;; Read only the selection that is active now. Never use _P because ZWCAD
  ;; keeps it as stale selection history even after everything is deselected.
  (setq ss (ssadd)
        doc (vla-get-ActiveDocument (vlax-get-acad-object))
        aset (vl-catch-all-apply 'vla-get-ActiveSelectionSet (list doc)))
  (if (not (vl-catch-all-error-p aset))
    (vlax-for obj aset
      (setq e (vlax-vla-object->ename obj))
      (if (wc:table-entity-p e) (ssadd e ss))))
  ;; Standard AutoCAD/ZWCAD implied-selection fallback, also current-only.
  (if (= (sslength ss) 0)
    (progn
      (setq raw (ssget "_I"))
      (if raw
        (progn
          (setq i 0)
          (repeat (sslength raw)
            (setq e (ssname raw i))
            (if (wc:table-entity-p e) (ssadd e ss))
            (setq i (1+ i)))))))
  (if (> (sslength ss) 0) ss nil))

(defun wc:current-implied-tables (/ raw ss i e)
  ;; Current PICKFIRST selection only; never consult stale _P/COM sets.
  (setq raw (ssget "_I") ss (ssadd))
  (if raw
    (progn
      (setq i 0)
      (repeat (sslength raw)
        (setq e (ssname raw i))
        (if (wc:table-entity-p e) (ssadd e ss))
        (setq i (1+ i)))))
  (if (> (sslength ss) 0) ss nil))

(defun wc:table-command (/ ss done)
  ;; Support both preselection and selection after starting WCNTT.
  ;; Capture current TABLEs before the first option prompt can disturb grips.
  (setq ss (wc:current-implied-tables))
  (if (not *wc-table-order-initialized*) (wc:ask-table-order))
  (setq done nil)
  (if ss
    (progn
      (wc:copy-selected-tables ss)
      (setq done T))
    (wc:clear-active-selection))
  (while (not done)
    (princ "\n클립보드로 복사할 TABLE 선택 <Enter=정렬옵션 변경>: ")
    (setq ss (ssget '((0 . "ACAD_TABLE"))))
    (if ss
      (progn
        (wc:copy-selected-tables ss)
        (setq done T))
      (wc:ask-table-order)))
  (princ))

(defun c:WCNT () (wc:run))
(defun c:WCNT2 () (wc:run))
(defun c:WCNTT () (wc:table-command))

;; WCNT needs noun/verb selection so tables can be selected before the command.
(if (= 0 (getvar "PICKFIRST")) (setvar "PICKFIRST" 1))

(princ "\nWINDOW_COUNT v31 로드 완료. 명령어: WCNT / TABLE집계: WCNTT")
(princ)
