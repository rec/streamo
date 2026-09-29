<?php
declare(strict_types=1);

require __DIR__ . '/../web/foto.php';

final class PartialWriteStream
{
    public $context;
    public static string $contents = '';
    public static ?int $truncatedTo = null;
    public static ?string $beforeTruncate = null;
    private int $position = 0;
    private bool $wrote = false;

    public function stream_open(string $path, string $mode, int $options, ?string &$openedPath): bool
    {
        return true;
    }

    public function stream_write(string $data): int
    {
        if ($this->wrote) return 0;
        $this->wrote = true;
        $part = substr($data, 0, 4);
        self::$contents = substr(self::$contents, 0, $this->position) . $part;
        $this->position += strlen($part);
        return strlen($part);
    }

    public function stream_tell(): int
    {
        return $this->position;
    }

    public function stream_seek(int $offset, int $whence = SEEK_SET): bool
    {
        $this->position = match ($whence) {
            SEEK_SET => $offset,
            SEEK_CUR => $this->position + $offset,
            SEEK_END => strlen(self::$contents) + $offset,
        };
        return true;
    }

    public function stream_truncate(int $size): bool
    {
        self::$truncatedTo = $size;
        self::$beforeTruncate = self::$contents;
        self::$contents = substr(self::$contents, 0, $size);
        return true;
    }

    public function stream_flush(): bool
    {
        return true;
    }
}

$previous = "{\"id\":1}\n";
PartialWriteStream::$contents = $previous;
stream_wrapper_register('partial-write', PartialWriteStream::class);
$manifest = fopen('partial-write://manifest', 'r+');
if ($manifest === false || fseek($manifest, 0, SEEK_END) !== 0) {
    throw new RuntimeException('Could not prepare partial-write manifest');
}
$target = $argv[1];
file_put_contents($target, 'uploaded photo');
$updated = append_manifest_record($manifest, "{\"id\":2}\n", $target);
fclose($manifest);

if ($updated || PartialWriteStream::$contents !== $previous
    || PartialWriteStream::$beforeTruncate !== $previous . '{"id'
    || PartialWriteStream::$truncatedTo !== strlen($previous) || file_exists($target)) {
    throw new RuntimeException('Failed append did not restore the manifest and image');
}
