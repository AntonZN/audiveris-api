#!/usr/bin/env bash
# Бэкап docker-проектов в S3. Один скрипт на сервер, по конфигу на проект:
#   projects/<проект>.env  →  <бакет>/<S3_PREFIX>/<проект>/{db,media}/...
#
# База (Postgres): дамп из контейнера через docker exec его же pg_dump, так что
# версия всегда совпадает → проверка, что дамп читается → db/daily/. Раз в неделю и раз
# в месяц та же копия уезжает в db/weekly/ и db/monthly/. В каждой папке
# остаются последние N дампов каждой базы.
#
# Медиа: зеркало media/<имя>/current/. Всё, что с диска удалили или
# перезаписали, не пропадает, а переезжает в media/<имя>/deleted/<дата>/ и
# живёт там MEDIA_KEEP_DELETED_DAYS дней.
#
# S3 работает через rclone в контейнере, на хост ставить ничего не нужно,
# кроме docker и curl (для уведомлений).

# shellcheck disable=SC2016 # sh-скрипты для контейнера — нарочно в одинарных кавычках
set -Eeuo pipefail
shopt -s inherit_errexit
umask 077

usage() {
	cat <<'EOF'
Использование:
  backup.sh run [проект...]       база + медиа (без аргументов — все projects/*.env)
  backup.sh db [проект...]        только база
  backup.sh media [проект...]     только медиа
  backup.sh check [проект...]     проверить доступ к S3, контейнеры и тома
  backup.sh install               check всех проектов и запуск каждую ночь через cron (от root)
  backup.sh list <проект>         что лежит в S3
  backup.sh restore-db <проект> [дамп|latest] [--db ИМЯ] [--yes]
  backup.sh restore-media <проект> <имя> <куда> [--from deleted/<папка>] [--yes]
EOF
}

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
BACKUP_HOME=${BACKUP_HOME:-${SELF%/*}}
# s3.env — общие настройки, лежит в git; secrets.env — ключи этого сервера, в git его нет.
for f in s3.env secrets.env; do
	if [[ -f $BACKUP_HOME/$f ]]; then
		# shellcheck source=/dev/null
		source "$BACKUP_HOME/$f"
	fi
done

: "${S3_BUCKET:?S3_BUCKET не задан в $BACKUP_HOME/s3.env}"
if [[ -z ${S3_ACCESS_KEY:-} || -z ${S3_SECRET_KEY:-} ]]; then
	echo "нет ключей S3. Создайте secrets.env и впишите S3_ACCESS_KEY и S3_SECRET_KEY:" >&2
	echo "  cp $BACKUP_HOME/secrets.env.example $BACKUP_HOME/secrets.env" >&2
	exit 2
fi
S3_PREFIX=${S3_PREFIX:-}
S3_PREFIX=${S3_PREFIX#/}
S3_PREFIX=${S3_PREFIX%/}
RCLONE_IMAGE=${RCLONE_IMAGE:-rclone/rclone:1.75.2}
WORK_DIR=${WORK_DIR:-/var/tmp/docker-backup}
BACKUP_HOST_LABEL=${BACKUP_HOST_LABEL:-$(hostname)}
BACKUP_CRON=${BACKUP_CRON:-30 3 * * *}

# Настройки rclone — через переменные окружения: docker run -e ИМЯ берёт
# значение отсюда, и ключи не светятся в `ps` аргументами командной строки.
export RCLONE_CONFIG_S3_TYPE=s3
export RCLONE_CONFIG_S3_PROVIDER=${S3_PROVIDER:-Other}
export RCLONE_CONFIG_S3_ACCESS_KEY_ID=$S3_ACCESS_KEY
export RCLONE_CONFIG_S3_SECRET_ACCESS_KEY=$S3_SECRET_KEY
export RCLONE_CONFIG_S3_ENDPOINT=${S3_ENDPOINT:-}
export RCLONE_CONFIG_S3_REGION=${S3_REGION:-}
# Ключу хватает прав на объекты, создавать бакет он не обязан.
export RCLONE_CONFIG_S3_NO_CHECK_BUCKET=true

PROJECT=""
RUN_DIR=""
RC_MOUNTS=() # -v для ближайших вызовов rc

log() { printf '%s %s%s\n' "$(date '+%F %T')" "${PROJECT:+[$PROJECT] }" "$*" >&2; }
die() {
	log "ОШИБКА: $*"
	exit 1
}

days_ago() {
	local t=$(($(date +%s) - $1 * 86400))
	date -d "@$t" +%F 2>/dev/null || date -r "$t" +%F
}

rc() {
	local extra=()
	read -ra extra <<<"${RCLONE_DOCKER_ARGS:-}"
	docker run --rm "${extra[@]}" "${RC_MOUNTS[@]}" \
		-e RCLONE_CONFIG_S3_TYPE -e RCLONE_CONFIG_S3_PROVIDER \
		-e RCLONE_CONFIG_S3_ACCESS_KEY_ID -e RCLONE_CONFIG_S3_SECRET_ACCESS_KEY \
		-e RCLONE_CONFIG_S3_ENDPOINT -e RCLONE_CONFIG_S3_REGION -e RCLONE_CONFIG_S3_NO_CHECK_BUCKET \
		"$RCLONE_IMAGE" --config /dev/null "$@"
}

# То же, но со stdin (списки файлов на удаление, rcat).
rc_in() {
	local extra=()
	read -ra extra <<<"${RCLONE_DOCKER_ARGS:-}"
	docker run --rm -i "${extra[@]}" "${RC_MOUNTS[@]}" \
		-e RCLONE_CONFIG_S3_TYPE -e RCLONE_CONFIG_S3_PROVIDER \
		-e RCLONE_CONFIG_S3_ACCESS_KEY_ID -e RCLONE_CONFIG_S3_SECRET_ACCESS_KEY \
		-e RCLONE_CONFIG_S3_ENDPOINT -e RCLONE_CONFIG_S3_REGION -e RCLONE_CONFIG_S3_NO_CHECK_BUCKET \
		"$RCLONE_IMAGE" --config /dev/null "$@"
}

new_run_dir() {
	mkdir -p "$WORK_DIR"
	# Хвосты прогонов, убитых kill -9: trap их не успел убрать.
	find "$WORK_DIR" -maxdepth 1 -name 'run.*' -mmin +1440 -exec rm -rf {} + 2>/dev/null || true
	RUN_DIR=$(mktemp -d "$WORK_DIR/run.XXXXXX")
	trap 'rm -rf "$RUN_DIR"' EXIT
}

all_projects() {
	local f
	for f in "$BACKUP_HOME"/projects/*.env; do
		[[ -e $f ]] && basename "$f" .env
	done
}

load_project() {
	local f="$BACKUP_HOME/projects/$1.env"
	[[ $1 =~ ^[A-Za-z0-9._-]+$ ]] || die "странное имя проекта: «$1»"
	[[ -f $f ]] || die "нет конфига $f"
	PROJECT=$1
	DB_CONTAINER="" DB_NAMES="" DB_USER="" DB_PASSWORD=""
	MEDIA=() MEDIA_EXCLUDE=() MEDIA_MAX_DELETE=""
	# shellcheck source=/dev/null
	source "$f"
	DB_KEEP_DAILY=${DB_KEEP_DAILY:-7}
	DB_KEEP_WEEKLY=${DB_KEEP_WEEKLY:-4}
	DB_KEEP_MONTHLY=${DB_KEEP_MONTHLY:-6}
	MEDIA_KEEP_DELETED_DAYS=${MEDIA_KEEP_DELETED_DAYS:-30}
	((DB_KEEP_DAILY >= 1)) || die "DB_KEEP_DAILY должен быть ≥ 1"
	DEST="s3:$S3_BUCKET${S3_PREFIX:+/$S3_PREFIX}/$PROJECT"
	export BK_USER=${DB_USER:-} BK_PASS=${DB_PASSWORD:-}
}

# ---------------------------------------------------------------- база ----

# Пользователь по умолчанию — POSTGRES_USER из переменных самого контейнера.
# Пароль не нужен: в официальном образе postgres вход через локальный сокет —
# trust. DB_USER/DB_PASSWORD из конфига приходят как BK_USER/BK_PASS.
PG_ENV='[ -n "$BK_PASS" ] && export PGPASSWORD="$BK_PASS"; U=${BK_USER:-${POSTGRES_USER:-postgres}};'

# dbx [-i] 'sh-скрипт' арг... — выполнить скрипт в контейнере базы
dbx() {
	local i=()
	if [[ $1 == -i ]]; then
		i=(-i)
		shift
	fi
	docker exec "${i[@]}" -e BK_USER -e BK_PASS "$DB_CONTAINER" sh -c "$1" sh "${@:2}"
}

require_running() {
	local state
	state=$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null) || die "контейнер $1 не найден (docker ps)"
	[[ $state == true ]] || die "контейнер $1 не запущен"
}

require_postgres() {
	require_running "$DB_CONTAINER"
	dbx 'command -v pg_dump' >/dev/null || die "в $DB_CONTAINER нет pg_dump — это точно контейнер Postgres?"
}

db_names() {
	if [[ -n $DB_NAMES ]]; then
		echo "$DB_NAMES"
	else
		dbx 'echo "${POSTGRES_DB:-${BK_USER:-${POSTGRES_USER:-postgres}}}"'
	fi
}

# custom-формат: уже сжат, восстанавливается pg_restore выборочно и в любую базу
dump_db() { dbx "$PG_ENV"' exec pg_dump -U "$U" -d "$1" --format=custom' "$1"; }

# Проверка, что дамп целый, до того как он вытеснит старые копии: pg_restore
# прочитывает его весь, расжимая каждый блок данных.
verify_dump() { dbx -i 'exec pg_restore --file=/dev/null' <"$1"; }

# Имена дампов базы в папке S3, от старых к новым. Несуществующая папка в S3 —
# это пустой список с кодом 0, так что любая ошибка тут настоящая (ключ, сеть)
# и должна ронять прогон, а не превращаться молча в «чистить нечего».
# Вызывать только как x=$(list_dumps ...): из < <(...) ошибка не дойдёт.
list_dumps() {
	local dir=$1 db=$2 names n
	names=$(rc lsf --files-only "$dir") || die "не прочитался список $dir"
	while IFS= read -r n; do
		if [[ $n == "${db}_"[0-9][0-9][0-9][0-9]-* ]]; then printf '%s\n' "$n"; fi
	done <<<"$names" | LC_ALL=C sort
}

prune_keep_last() {
	local dir=$1 db=$2 keep=$3 names all=() extra
	names=$(list_dumps "$dir" "$db")
	[[ -z $names ]] || mapfile -t all <<<"$names"
	extra=$((${#all[@]} - keep))
	((extra > 0)) || return 0
	printf '%s\n' "${all[@]:0:extra}" | rc_in delete "$dir" --files-from-raw -
	log "база $db: из ${dir##*/} удалено старых дампов: $extra"
}

# Свежий дамп становится недельным, если последнему недельному ≥ 7 дней,
# и месячным, если в этом месяце месячного ещё нет. Так пропущенное
# воскресенье или первое число не оставляют дыру, а первый же прогон сразу
# кладёт по копии во все три папки.
promote() {
	local db=$1 file=$2 newest monthly
	if ((DB_KEEP_WEEKLY > 0)); then
		newest=$(list_dumps "$DEST/db/weekly" "$db" | tail -n1)
		local d=${newest#"${db}_"}
		if [[ -z $newest || ${d:0:10} < $(days_ago 6) ]]; then
			rc copyto "$DEST/db/daily/$file" "$DEST/db/weekly/$file"
			log "база $db: копия в weekly"
		fi
	fi
	if ((DB_KEEP_MONTHLY > 0)); then
		monthly=$(list_dumps "$DEST/db/monthly" "$db")
		if [[ $'\n'$monthly != *$'\n'"${db}_$(date +%Y-%m)-"* ]]; then
			rc copyto "$DEST/db/daily/$file" "$DEST/db/monthly/$file"
			log "база $db: копия в monthly"
		fi
	fi
}

backup_one_db() {
	local db=$1 file
	file="${db}_$(date +%F_%H%M%S).dump"
	log "база $db: дамп"
	dump_db "$db" >"$RUN_DIR/$file"
	[[ -s $RUN_DIR/$file ]] || die "дамп $db пустой"
	verify_dump "$RUN_DIR/$file"
	RC_MOUNTS=(-v "$RUN_DIR:/dump:ro")
	rc copyto "/dump/$file" "$DEST/db/daily/$file"
	RC_MOUNTS=()
	log "база $db: залито db/daily/$file ($(du -h "$RUN_DIR/$file" | cut -f1))"
	rm -f "$RUN_DIR/$file"
	promote "$db" "$file"
	prune_keep_last "$DEST/db/daily" "$db" "$DB_KEEP_DAILY"
	prune_keep_last "$DEST/db/weekly" "$db" "$DB_KEEP_WEEKLY"
	prune_keep_last "$DEST/db/monthly" "$db" "$DB_KEEP_MONTHLY"
}

backup_db() {
	if [[ -z $DB_CONTAINER ]]; then
		log "база: DB_CONTAINER не задан — пропускаю"
		return 0
	fi
	require_postgres
	local db dbs=()
	read -ra dbs <<<"$(db_names)"
	((${#dbs[@]})) || die "не понял, какие базы дампить — задайте DB_NAMES"
	for db in "${dbs[@]}"; do backup_one_db "$db"; done
}

# --------------------------------------------------------------- медиа ----

# «имя=/путь/на/хосте» или «имя=том[:/подпапка]» → M_NAME, M_PATH, RC_MOUNTS
parse_media() {
	local spec=$1 mode=$2 src vol sub=""
	[[ $spec == ?*=?* ]] || die "MEDIA: ждали «имя=источник», получили «$spec»"
	M_NAME=${spec%%=*}
	src=${spec#*=}
	if [[ $src == /* ]]; then
		[[ $mode == rw ]] && mkdir -p "$src"
		[[ -d $src ]] || die "медиа $M_NAME: нет каталога $src"
		RC_MOUNTS=(-v "$src:/src:$mode")
		M_PATH=/src
	else
		vol=${src%%:*}
		[[ $src == *:* ]] && sub=${src#*:}
		sub=${sub#/}
		# Без этой проверки docker run молча создал бы пустой том, и sync
		# «удалил» бы всё зеркало.
		docker volume inspect "$vol" >/dev/null 2>&1 || die "медиа $M_NAME: нет docker-тома $vol (docker volume ls)"
		RC_MOUNTS=(-v "$vol:/src:$mode")
		M_PATH=/src${sub:+/$sub}
	fi
}

prune_deleted() {
	local dir=$1 cutoff d dirs
	cutoff=$(days_ago "$MEDIA_KEEP_DELETED_DAYS")
	dirs=$(rc lsf --dirs-only "$dir") || die "не прочитался список $dir"
	while IFS= read -r d; do
		d=${d%/}
		[[ $d =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2} ]] || continue
		if [[ ${d:0:10} < $cutoff ]]; then
			rc purge "$dir/$d"
			log "медиа $M_NAME: удалён архив deleted/$d"
		fi
	done <<<"$dirs"
}

sync_media() {
	local stamp=$2 dst entries excl=() e
	parse_media "$1" ro
	dst="$DEST/media/$M_NAME"
	# Пустой источник — почти наверняка не тот путь или том, а не «всё удалили».
	entries=$(rc lsf --max-depth 1 "$M_PATH") || die "медиа $M_NAME: не читается $M_PATH"
	[[ -n $entries ]] || die "медиа $M_NAME: $M_PATH пуст — не синхронизирую, проверьте путь"
	for e in ${MEDIA_EXCLUDE[@]+"${MEDIA_EXCLUDE[@]}"}; do excl+=(--exclude "$e"); done
	log "медиа $M_NAME: синхронизация → media/$M_NAME/current"
	# --update --use-server-modtime: сравнение по времени заливки из листинга,
	# без HEAD-запроса и без хэширования на каждый файл.
	rc sync "$M_PATH" "$dst/current" \
		--backup-dir "$dst/deleted/$stamp" \
		--fast-list --update --use-server-modtime \
		${MEDIA_MAX_DELETE:+--max-delete "$MEDIA_MAX_DELETE"} \
		"${excl[@]}" \
		--stats 1h --stats-log-level NOTICE
	RC_MOUNTS=()
	prune_deleted "$dst/deleted"
}

backup_media() {
	if ((${#MEDIA[@]} == 0)); then
		log "медиа: MEDIA не задан — пропускаю"
		return 0
	fi
	local spec stamp
	stamp=$(date +%F_%H%M%S)
	for spec in "${MEDIA[@]}"; do sync_media "$spec" "$stamp"; done
}

# ------------------------------------------------------------- прогон ----

take_lock() {
	if ! command -v flock >/dev/null; then
		log "flock не найден — работаю без защиты от двойного запуска"
		return 0
	fi
	exec 9>"$WORK_DIR/.lock"
	flock -n 9 || die "предыдущий бэкап ещё идёт ($WORK_DIR/.lock)"
}

hc_ping() {
	[[ -n ${HEALTHCHECK_URL:-} ]] || return 0
	curl -fsS -m 10 --retry 3 -o /dev/null "${HEALTHCHECK_URL%/}$1" || log "healthcheck: не достучался"
}

# notify 'текст' [файл] — в Telegram; с файлом уходит документом с подписью.
notify() {
	[[ -n ${TG_BOT_TOKEN:-} && -n ${TG_CHAT_ID:-} ]] || return 0
	local method=sendMessage args=(--data-urlencode "chat_id=$TG_CHAT_ID" --data-urlencode "text=$1")
	if [[ -n ${2:-} ]]; then
		method=sendDocument
		args=(-F "chat_id=$TG_CHAT_ID" -F "caption=$1" -F "document=@$2")
	fi
	# URL с токеном идёт через stdin (-K -), чтобы не светиться в `ps`.
	printf 'url = "https://api.telegram.org/bot%s/%s"\n' "$TG_BOT_TOKEN" "$method" |
		curl -fsS -m 30 -o /dev/null -K - "${args[@]}" || log "telegram: сообщение не ушло"
}

backup_project() {
	trap 'log "упала команда (код $?): $BASH_COMMAND"' ERR
	load_project "$1"
	case $2 in
	run)
		backup_db
		backup_media
		;;
	db) backup_db ;;
	media) backup_media ;;
	esac
	log "готово"
}

cmd_backup() {
	local what=$1 projects=("${@:2}") p status failed=() t0=$SECONDS
	((${#projects[@]})) || mapfile -t projects < <(all_projects)
	((${#projects[@]})) || die "нет ни одного проекта: $BACKUP_HOME/projects/*.env"
	new_run_dir
	take_lock
	[[ $what == run ]] && hc_ping /start
	for p in "${projects[@]}"; do
		set +e
		(
			set -e
			backup_project "$p" "$what"
		) 2>&1 | tee "$RUN_DIR/$p.log"
		status=${PIPESTATUS[0]}
		set -e
		((status == 0)) || failed+=("$p")
	done
	if ((${#failed[@]})); then
		for p in "${failed[@]}"; do cat "$RUN_DIR/$p.log"; done >"$RUN_DIR/failed.log"
		log "НЕ УДАЛОСЬ: ${failed[*]}"
		notify "❌ Бэкап ($what) на $BACKUP_HOST_LABEL: упали ${failed[*]} из ${#projects[@]}. Лог во вложении." "$RUN_DIR/failed.log"
		[[ $what == run ]] && hc_ping /fail
		return 1
	fi
	log "все проекты (${projects[*]}) за $((SECONDS - t0)) с"
	if [[ ${NOTIFY_ON_SUCCESS:-0} == 1 ]]; then
		notify "✅ Бэкап ($what) на $BACKUP_HOST_LABEL: ${projects[*]} за $((SECONDS - t0)) с"
	fi
	if [[ $what == run ]]; then hc_ping ""; fi
}

# ------------------------------------------------------- обслуживание ----

check_project() {
	local spec names
	load_project "$1"
	echo "— $1 → ${DEST#s3:}"
	if [[ -n $DB_CONTAINER ]]; then
		require_postgres
		names=$(db_names)
		[[ -n $names ]] || die "не понял, какие базы дампить — задайте DB_NAMES"
		echo "  ✓ база: $DB_CONTAINER, базы: $names"
	else
		echo "  · база: не настроена"
	fi
	for spec in ${MEDIA[@]+"${MEDIA[@]}"}; do
		parse_media "$spec" ro
		[[ -n $(rc lsf --max-depth 1 "$M_PATH") ]] || die "медиа $M_NAME: ${spec#*=} пуст или не читается"
		echo "  ✓ медиа $M_NAME: ${spec#*=}"
	done
}

cmd_check() {
	local projects=("$@") p bad=0 status root="$S3_BUCKET${S3_PREFIX:+/$S3_PREFIX}"
	((${#projects[@]})) || mapfile -t projects < <(all_projects)
	if echo ok | rc_in rcat "s3:$root/.backup-check" && rc deletefile "s3:$root/.backup-check"; then
		echo "✓ S3: запись и удаление в $root работают"
	else
		echo "✗ S3: не получилось записать в $root"
		bad=1
	fi
	for p in "${projects[@]}"; do
		# Подоболочка не в if: так set -e внутри работает, а die одного
		# проекта не обрывает проверку остальных.
		set +e
		(
			set -e
			check_project "$p"
		)
		status=$?
		set -e
		((status == 0)) || bad=1
	done
	return "$bad"
}

cmd_install() {
	local cron=/etc/cron.d/docker-backup log=/var/log/docker-backup.log status
	[[ -w ${cron%/*} ]] || die "нет прав на ${cron%/*} — запустите от root"
	set +e
	(
		set -e
		cmd_check
	)
	status=$?
	set -e
	((status == 0)) || die "проверка не прошла — в cron не поставлено"
	[[ ! -f $BACKUP_HOME/secrets.env ]] || chmod 600 "$BACKUP_HOME/secrets.env"
	# PATH — как сейчас: у cron свой куцый, и docker из /usr/local/bin или snap он не найдёт.
	{
		echo "# Бэкап docker-проектов в S3. Поставлено: $SELF install"
		echo "PATH=$PATH"
		echo "BACKUP_HOME=$BACKUP_HOME"
		printf '%s root %q run >> %s 2>&1\n' "$BACKUP_CRON" "$SELF" "$log"
	} >"$cron"
	chmod 644 "$cron"
	echo "✓ cron: $cron, расписание «$BACKUP_CRON», лог — $log"
}

cmd_list() {
	[[ $# -eq 1 ]] || die "использование: backup.sh list <проект>"
	load_project "$1"
	local n
	echo "== ${DEST#s3:}/db"
	rc lsl "$DEST/db" 2>/dev/null | sort -k4 || true
	while IFS= read -r n; do
		n=${n%/}
		echo "== ${DEST#s3:}/media/$n"
		rc size "$DEST/media/$n/current" 2>/dev/null | sed 's/^/current: /' || true
		rc lsf --dirs-only "$DEST/media/$n/deleted" 2>/dev/null | sed 's/^/deleted\//' || true
	done < <(rc lsf --dirs-only "$DEST/media" 2>/dev/null || true)
}

confirm() {
	local answer
	printf '%s\nВведите «%s» для подтверждения: ' "$1" "$2" >&2
	read -r answer
	[[ $answer == "$2" ]] || die "отменено"
}

cmd_restore_db() {
	local project="" file="" target="" yes=0 dbs=() db tier names path=""
	while (($#)); do
		case $1 in
		--db) target=$2 && shift ;;
		--yes) yes=1 ;;
		*) if [[ -z $project ]]; then project=$1; elif [[ -z $file ]]; then file=$1; else die "лишний аргумент: $1"; fi ;;
		esac
		shift
	done
	[[ -n $project ]] || die "использование: backup.sh restore-db <проект> [дамп|latest] [--db ИМЯ] [--yes]"
	load_project "$project"
	[[ -n $DB_CONTAINER ]] || die "в projects/$project.env не задан DB_CONTAINER"
	require_postgres
	if [[ -z $file || $file == latest ]]; then
		read -ra dbs <<<"$(db_names)"
		((${#dbs[@]} == 1)) || die "баз несколько (${dbs[*]}) — укажите дамп явно (backup.sh list $project)"
		file=$(list_dumps "$DEST/db/daily" "${dbs[0]}" | tail -n1)
		[[ -n $file ]] || die "в db/daily нет дампов ${dbs[0]}"
		path=daily/$file
	elif [[ $file == */* ]]; then
		path=$file
		file=${file##*/}
	else
		for tier in daily weekly monthly; do
			names=$(rc lsf --files-only "$DEST/db/$tier") || die "не прочитался список db/$tier"
			if grep -qxF "$file" <<<"$names"; then
				path=$tier/$file
				break
			fi
		done
		[[ -n $path ]] || die "дамп $file не найден (backup.sh list $project)"
	fi
	db=${file%%_[0-9][0-9][0-9][0-9]-*}
	target=${target:-$db}
	((yes)) || confirm "Восстановить db/$path в базу «$target» контейнера $DB_CONTAINER? Её текущие данные будут перезаписаны." "$target"

	new_run_dir
	log "качаю db/$path"
	RC_MOUNTS=(-v "$RUN_DIR:/dump")
	rc copyto "$DEST/db/$path" "/dump/$file"
	RC_MOUNTS=()
	log "восстанавливаю в «$target»"
	# Через файл в контейнере, а не stdin: pg_restore с --clean на
	# неперематываемом входе может не найти блок данных.
	docker cp "$RUN_DIR/$file" "$DB_CONTAINER:/tmp/$file"
	dbx "$PG_ENV"' createdb -U "$U" "$1" 2>/dev/null
		pg_restore -U "$U" -d "$1" --clean --if-exists --no-owner --no-privileges --single-transaction "/tmp/$2"
		s=$?; rm -f "/tmp/$2"; exit $s' "$target" "$file"
	log "готово: db/$path → «$target»"
}

cmd_restore_media() {
	local args=() from=current yes=0 name dest spec
	while (($#)); do
		case $1 in
		--from) from=${2%/} && shift ;;
		--yes) yes=1 ;;
		*) args+=("$1") ;;
		esac
		shift
	done
	((${#args[@]} == 3)) || die "использование: backup.sh restore-media <проект> <имя> <куда> [--from deleted/<папка>] [--yes]"
	load_project "${args[0]}"
	name=${args[1]} dest=${args[2]}
	spec="$name=$dest"
	((yes)) || confirm "Скопировать media/$name/$from в $dest? Одноимённые файлы будут перезаписаны, лишние останутся." "$name"
	parse_media "$spec" rw
	log "медиа $name: media/$name/$from → $dest"
	rc copy "$DEST/media/$name/$from" "$M_PATH" --fast-list --stats 1h --stats-log-level NOTICE
	RC_MOUNTS=()
	log "готово. Файлы записаны от root — если приложение работает не от root, сделайте chown."
}

main() {
	local cmd=${1:-}
	(($#)) && shift
	case $cmd in
	run | db | media) cmd_backup "$cmd" "$@" ;;
	check) cmd_check "$@" ;;
	install) cmd_install ;;
	list) cmd_list "$@" ;;
	restore-db) cmd_restore_db "$@" ;;
	restore-media) cmd_restore_media "$@" ;;
	-h | --help | help) usage ;;
	*)
		usage >&2
		exit 2
		;;
	esac
}

main "$@"
